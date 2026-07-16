"""SQLite-backed artifact metadata with staged write and immutable publication.

The repository **constructs** the :class:`Artifact` record server-side from the
actual write result, never trusting caller-provided hash/size/path.  The staged
approach is:

1. Validate session/task/planner existence and ownership (same session).
2. Check quotas (per-artifact + session-level) inside the transaction.
3. Check no duplicate artifact_id exists.
4. Call ``store.write_temp`` + ``store.publish`` to get the final file.
5. INSERT metadata with server-computed fields.
6. On DB failure, ``store.rollback_publish`` removes the final file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import aiosqlite
from pydantic import TypeAdapter

from protocol import ArtifactType, EntityId
from storage.artifact_store import ArtifactStore
from storage.db import Database, utc_now_text
from storage.errors import (
    ArtifactNotFound,
    ContainmentViolation,
    PathEscapeError,
    QuotaExceeded,
    RecordNotFound,
)

_ENTITY_ID = TypeAdapter(EntityId)


def _entity_id(value: str) -> str:
    return _ENTITY_ID.validate_python(value)


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """Read-only projection of a persisted artifact row."""

    artifact_id: str
    session_id: str
    task_id: str | None
    planner_run_id: str | None
    artifact_type: str
    relative_path: str
    sha256: str
    size_bytes: int
    redacted: bool
    created_at: str


class ArtifactRepository:
    """CRUD operations for artifact metadata in SQLite.

    Parameters
    ----------
    database:
        Shared :class:`Database` handle.
    store:
        File-system store for staged writes and containment verification.
    max_artifact_bytes:
        Per-artifact size ceiling.
    max_session_artifact_bytes:
        Cumulative session-level size ceiling.
    """

    def __init__(
        self,
        database: Database,
        store: ArtifactStore,
        *,
        max_artifact_bytes: int = 100 * 1024 * 1024,
        max_session_artifact_bytes: int = 1024 * 1024 * 1024,
    ) -> None:
        if max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be positive")
        if max_session_artifact_bytes < 1:
            raise ValueError("max_session_artifact_bytes must be positive")
        self._database = database
        self._store = store
        self._max_artifact_bytes = max_artifact_bytes
        self._max_session_artifact_bytes = max_session_artifact_bytes

    @property
    def store(self) -> ArtifactStore:
        return self._store

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        artifact_id: str,
        session_id: str,
        artifact_type: ArtifactType,
        content: bytes,
        task_id: str | None = None,
        planner_run_id: str | None = None,
        redacted: bool = True,
        now: datetime | None = None,
    ) -> ArtifactRecord:
        """Create an artifact with server-side metadata construction.

        Steps:
        1. Validate IDs and owner exclusivity.
        2. Stage file write (compute hash/size, write temp, fsync, set perms).
        3. In ``BEGIN IMMEDIATE`` transaction: verify session/task/planner
           existence and ownership, check quotas, check no duplicate, publish
           final file, INSERT metadata.
        4. On DB failure, rollback the published file.

        Returns the server-constructed :class:`ArtifactRecord`.
        """
        # Validate IDs
        resolved_id = _entity_id(artifact_id)
        resolved_session = _entity_id(session_id)
        resolved_task = _entity_id(task_id) if task_id else None
        resolved_planner = _entity_id(planner_run_id) if planner_run_id else None

        # Owner exclusivity
        if resolved_task is not None and resolved_planner is not None:
            raise ValueError("task_id and planner_run_id are mutually exclusive")

        # Stage file write first (outside transaction for I/O)
        staged = self._store.write_temp(resolved_id, artifact_type.value, content)
        published = False

        timestamp = utc_now_text(now)

        try:
            async with self._database.immediate_transaction() as tx:
                # Verify session exists
                session_row = await tx.fetch_one(
                    "SELECT id FROM sessions WHERE id = ?",
                    (resolved_session,),
                )
                if session_row is None:
                    raise RecordNotFound(f"session not found: {resolved_session}")

                # Verify task exists AND belongs to same session
                if resolved_task is not None:
                    task_row = await tx.fetch_one(
                        "SELECT t.id, t.node_run_id FROM tasks t "
                        "JOIN node_runs nr ON t.node_run_id = nr.id "
                        "WHERE t.id = ?",
                        (resolved_task,),
                    )
                    if task_row is None:
                        raise RecordNotFound(f"task not found: {resolved_task}")
                    # Verify task's node_run belongs to same session
                    nr_row = await tx.fetch_one(
                        "SELECT wr.session_id FROM node_runs nr "
                        "JOIN workflow_runs wr ON nr.workflow_run_id = wr.id "
                        "WHERE nr.id = ?",
                        (task_row["node_run_id"],),
                    )
                    if nr_row is None or str(nr_row["session_id"]) != resolved_session:
                        raise ContainmentViolation(
                            f"task {resolved_task} does not belong to session {resolved_session}"
                        )

                # Verify planner run exists AND belongs to same session
                if resolved_planner is not None:
                    pr_row = await tx.fetch_one(
                        "SELECT session_id FROM planner_runs WHERE id = ?",
                        (resolved_planner,),
                    )
                    if pr_row is None:
                        raise RecordNotFound(f"planner run not found: {resolved_planner}")
                    if str(pr_row["session_id"]) != resolved_session:
                        raise ContainmentViolation(
                            f"planner run {resolved_planner} does not belong "
                            f"to session {resolved_session}"
                        )

                # Per-artifact quota
                if staged.size_bytes > self._max_artifact_bytes:
                    raise QuotaExceeded(
                        f"artifact size {staged.size_bytes} exceeds "
                        f"per-artifact limit {self._max_artifact_bytes}"
                    )

                # Session-level quota (atomic within transaction)
                row = await tx.fetch_one(
                    "SELECT COALESCE(SUM(size_bytes), 0) AS total "
                    "FROM artifacts WHERE session_id = ?",
                    (resolved_session,),
                )
                current_total = int(row["total"]) if row else 0
                if current_total + staged.size_bytes > self._max_session_artifact_bytes:
                    raise QuotaExceeded(
                        f"session {resolved_session} artifact quota exceeded: "
                        f"{current_total} + {staged.size_bytes} > "
                        f"{self._max_session_artifact_bytes}"
                    )

                # Check no duplicate artifact_id
                existing = await tx.fetch_one(
                    "SELECT id FROM artifacts WHERE id = ?",
                    (resolved_id,),
                )
                if existing is not None:
                    raise PathEscapeError(
                        f"artifact already exists: {resolved_id}; immutable, cannot overwrite"
                    )

                # Publish final file (no-overwrite)
                self._store.publish(staged)
                published = True

                # Store relative path from store base
                try:
                    final_path = self._store.resolve(staged.artifact_id, staged.artifact_type)
                    relative = final_path.relative_to(self._store.base_dir)
                    relative_str = relative.as_posix()
                except ValueError:
                    relative_str = final_path.as_posix()

                # Insert metadata with server-computed fields
                await tx.execute(
                    """
                    INSERT INTO artifacts(
                        id, session_id, task_id, planner_run_id,
                        artifact_type, relative_path, sha256, size_bytes,
                        redacted, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved_id,
                        resolved_session,
                        resolved_task,
                        resolved_planner,
                        artifact_type.value,
                        relative_str,
                        staged.sha256,
                        staged.size_bytes,
                        1 if redacted else 0,
                        timestamp,
                    ),
                )

        except BaseException:
            # DB failed — cleanup best-effort, never mask the original error
            try:
                if published:
                    self._store.rollback_publish(staged)
                self._store.discard_staged(staged)
            except Exception as cleanup_err:
                import logging

                logging.getLogger(__name__).warning(
                    "cleanup after DB failure failed: %s", cleanup_err
                )
            raise

        # DB committed — finalize the handle to prevent stale rollback
        self._store.finalize(staged)

        return ArtifactRecord(
            artifact_id=resolved_id,
            session_id=resolved_session,
            task_id=resolved_task,
            planner_run_id=resolved_planner,
            artifact_type=artifact_type.value,
            relative_path=relative_str,
            sha256=staged.sha256,
            size_bytes=staged.size_bytes,
            redacted=redacted,
            created_at=timestamp,
        )

    # ------------------------------------------------------------------
    # Read path (with re-verification)
    # ------------------------------------------------------------------

    async def get(self, artifact_id: str) -> ArtifactRecord:
        """Fetch artifact metadata by ID."""
        resolved_id = _entity_id(artifact_id)
        async with self._database.connection() as conn:
            cursor = await conn.execute("SELECT * FROM artifacts WHERE id = ?", (resolved_id,))
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            raise ArtifactNotFound(f"artifact not found: {resolved_id}")
        return self._to_record(row)

    async def get_and_verify(
        self,
        artifact_id: str,
        *,
        expected_session_id: str,
        expected_task_id: str | None = None,
    ) -> tuple[ArtifactRecord, bytes]:
        """Fetch metadata, verify ownership, re-verify containment and read.

        The caller must pass ``expected_session_id`` so cross-session access
        is rejected.  If ``expected_task_id`` is set, the artifact must
        belong to that task.

        Returns ``(record, content_bytes)``.
        """
        record = await self.get(artifact_id)

        # Session ownership check
        if record.session_id != expected_session_id:
            raise ArtifactNotFound(
                f"artifact {artifact_id} does not belong to session {expected_session_id}"
            )

        # Task ownership check (if requested and artifact is task-scoped)
        if (
            expected_task_id is not None
            and record.task_id is not None
            and record.task_id != expected_task_id
        ):
            raise ArtifactNotFound(
                f"artifact {artifact_id} does not belong to task {expected_task_id}"
            )

        # Re-verify on disk
        content = self._store.read_bytes(artifact_id, record.artifact_type)

        # Hash re-verification
        from hashlib import sha256 as _sha256

        actual_hash = _sha256(content).hexdigest()
        if actual_hash != record.sha256:
            raise ContainmentViolation(
                f"artifact {artifact_id} hash mismatch: expected {record.sha256}, got {actual_hash}"
            )

        # Size re-verification
        if len(content) != record.size_bytes:
            raise ContainmentViolation(
                f"artifact {artifact_id} size mismatch: "
                f"expected {record.size_bytes}, got {len(content)}"
            )

        return record, content

    async def list_by_session(self, session_id: str) -> list[ArtifactRecord]:
        """List all artifacts for a session."""
        resolved_id = _entity_id(session_id)
        async with self._database.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM artifacts WHERE session_id = ? ORDER BY created_at",
                (resolved_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._to_record(r) for r in rows]

    async def list_by_task(self, task_id: str) -> list[ArtifactRecord]:
        """List all artifacts for a task."""
        resolved_id = _entity_id(task_id)
        async with self._database.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at",
                (resolved_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._to_record(r) for r in rows]

    async def session_total_bytes(self, session_id: str) -> int:
        """Return the total bytes of all artifacts in a session."""
        resolved_id = _entity_id(session_id)
        async with self._database.connection() as conn:
            cursor = await conn.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM artifacts WHERE session_id = ?",
                (resolved_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return int(row["total"]) if row else 0

    async def delete(self, artifact_id: str) -> None:
        """Delete artifact metadata and file."""
        resolved_id = _entity_id(artifact_id)
        record = await self.get(resolved_id)
        async with self._database.immediate_transaction() as tx:
            await tx.execute("DELETE FROM artifacts WHERE id = ?", (resolved_id,))
        # File cleanup after DB commit
        self._store.delete(resolved_id, record.artifact_type)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_record(row: aiosqlite.Row) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=str(row["id"]),
            session_id=str(row["session_id"]),
            task_id=(str(row["task_id"]) if row["task_id"] is not None else None),
            planner_run_id=(
                str(row["planner_run_id"]) if row["planner_run_id"] is not None else None
            ),
            artifact_type=str(row["artifact_type"]),
            relative_path=str(row["relative_path"]),
            sha256=str(row["sha256"]),
            size_bytes=int(row["size_bytes"]),
            redacted=bool(row["redacted"]),
            created_at=str(row["created_at"]),
        )
