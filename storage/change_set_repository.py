"""Durable canonical ChangeSet storage with fenced CAS transitions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Self

import aiosqlite
from pydantic import Field, model_validator

from protocol import (
    ActorType,
    ArtifactRef,
    ArtifactType,
    ChangeSet,
    ChangeSetStatus,
    CompiledGraph,
    FrozenStrictModel,
    NodeType,
    RepoRelativePath,
    SecuritySeverity,
    TaskStatus,
    canonical_json,
)
from storage.artifact_repository import (
    ArtifactRecord,
    ArtifactRepository,
    StagedArtifact,
)
from storage.db import Database, Transaction, utc_now_text
from storage.errors import ChangeSetIntegrityError, ConcurrencyConflict, RecordNotFound
from storage.event_repository import EventRepository
from storage.leases import MasterLease, MasterLeaseRepository, WorkspaceLease
from workflow.events import (
    CHANGE_SET_STATE_CHANGED,
    TASK_STATE_CHANGED,
    ChangeSetEventPayload,
    TaskEventPayload,
)
from workspace.change_set import ChangeSetManifest, FileAction
from workspace.lock_manager import LockManager
from workspace.transaction import CapturedWorkspaceChangeSet, FilePreimage

_LOGGER = logging.getLogger(__name__)

_TRANSITIONS: dict[ChangeSetStatus, frozenset[ChangeSetStatus]] = {
    ChangeSetStatus.CAPTURED: frozenset(
        {
            ChangeSetStatus.GUARD_PASSED,
            ChangeSetStatus.GUARD_REJECTED,
            ChangeSetStatus.ABANDONED_PARTIAL,
            ChangeSetStatus.QUARANTINED,
            ChangeSetStatus.CANCELLED,
        }
    ),
    ChangeSetStatus.GUARD_PASSED: frozenset(
        {
            ChangeSetStatus.TEST_PASSED,
            ChangeSetStatus.TEST_FAILED,
            ChangeSetStatus.STALE,
            ChangeSetStatus.CANCELLED,
        }
    ),
    ChangeSetStatus.TEST_PASSED: frozenset(
        {
            ChangeSetStatus.PENDING_APPROVAL,
            ChangeSetStatus.POLICY_REJECTED,
            ChangeSetStatus.STALE,
            ChangeSetStatus.CANCELLED,
        }
    ),
    ChangeSetStatus.PENDING_APPROVAL: frozenset(
        {
            ChangeSetStatus.APPROVED,
            ChangeSetStatus.REJECTED,
            ChangeSetStatus.STALE,
            ChangeSetStatus.CANCELLED,
        }
    ),
    ChangeSetStatus.APPROVED: frozenset(
        {
            ChangeSetStatus.MERGED,
            ChangeSetStatus.STALE,
            ChangeSetStatus.CANCELLED,
        }
    ),
}


class EvidenceBinding(FrozenStrictModel):
    kind: str = Field(pattern=r"^(status|staged|unstaged)$")
    artifact_ref: ArtifactRef


class PreimageBinding(FrozenStrictModel):
    path: RepoRelativePath
    artifact_ref: ArtifactRef
    mode: int = Field(ge=0, le=0o777)
    baseline_ignored: bool


class StoredChangeSetDocument(FrozenStrictModel):
    manifest: ChangeSetManifest
    canonical_patch_ref: ArtifactRef
    evidence: tuple[EvidenceBinding, ...] = Field(min_length=3, max_length=3)
    preimages: tuple[PreimageBinding, ...] = Field(default_factory=tuple, max_length=500)

    @model_validator(mode="after")
    def validate_bindings(self) -> Self:
        if {item.kind for item in self.evidence} != {"status", "staged", "unstaged"}:
            raise ValueError("ChangeSet evidence must contain status, staged, and unstaged")
        paths = [item.path for item in self.preimages]
        if len(paths) != len(set(paths)):
            raise ValueError("ChangeSet preimage paths must be unique")
        return self


class SecurityChangeSetPayload(FrozenStrictModel):
    change_set_id: str = Field(min_length=1, max_length=128)
    previous_status: ChangeSetStatus | None = None
    status: ChangeSetStatus
    reason: str = Field(min_length=1, max_length=1_000)
    master_fencing_token: int = Field(ge=1)
    workspace_fencing_token: int | None = Field(default=None, ge=1)


@dataclass(frozen=True, slots=True)
class ChangeSetRecord:
    change_set: ChangeSet
    document: StoredChangeSetDocument
    created_at: str
    updated_at: str


def _artifact_ref(record: ArtifactRecord) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=record.artifact_id,
        artifact_type=ArtifactType(record.artifact_type),
        relative_path=record.relative_path,
        sha256=record.sha256,
        size_bytes=record.size_bytes,
    )


class ChangeSetRepository:
    """Persist captured workspace changes and advance their durable state."""

    def __init__(
        self,
        database: Database,
        artifacts: ArtifactRepository,
        events: EventRepository,
        master_leases: MasterLeaseRepository,
        locks: LockManager,
    ) -> None:
        self._database = database
        self._artifacts = artifacts
        self._events = events
        self._master_leases = master_leases
        self._locks = locks

    async def persist_capture(
        self,
        *,
        session_id: str,
        workflow_run_id: str,
        node_run_id: str,
        task_id: str,
        capture: CapturedWorkspaceChangeSet,
        master_lease: MasterLease,
        workspace_lease: WorkspaceLease,
        task_target: TaskStatus,
        task_error_code: str | None = None,
        status: ChangeSetStatus = ChangeSetStatus.CAPTURED,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> ChangeSetRecord:
        """Store one canonical capture and bind it to its AgentTask node."""

        if status not in {
            ChangeSetStatus.CAPTURED,
            ChangeSetStatus.ABANDONED_PARTIAL,
        }:
            raise ValueError("capture status must be captured or abandoned_partial")
        terminal_targets = {
            TaskStatus.SUCCEEDED,
            TaskStatus.PRIVILEGE_REQUESTED,
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
            TaskStatus.CANCELLED,
            TaskStatus.BLOCKED_BY_GUARD,
            TaskStatus.PARSE_FAILED,
            TaskStatus.ORPHANED,
        }
        if task_target not in terminal_targets:
            raise ValueError("capture must atomically finalize its write task")
        if task_target == TaskStatus.SUCCEEDED:
            if status != ChangeSetStatus.CAPTURED or task_error_code is not None:
                raise ValueError("successful write task requires a captured ChangeSet")
        elif status != ChangeSetStatus.ABANDONED_PARTIAL:
            raise ValueError("failed write task requires an abandoned ChangeSet")
        elif not task_error_code:
            raise ValueError("failed write task requires an error code")
        patch_sha256 = sha256(capture.patch_bytes).hexdigest()
        evidence = {
            "status": capture.status_evidence,
            "staged": capture.staged_evidence,
            "unstaged": capture.unstaged_evidence,
        }
        expected_evidence = {
            "status": capture.manifest.status_evidence_sha256,
            "staged": capture.manifest.staged_evidence_sha256,
            "unstaged": capture.manifest.unstaged_evidence_sha256,
        }
        for kind, content in evidence.items():
            if sha256(content).hexdigest() != expected_evidence[kind]:
                raise ChangeSetIntegrityError(f"captured {kind} evidence hash mismatch")

        change_set_id = _stable_id(
            "changeset",
            task_id,
            capture.manifest.base_commit,
            capture.manifest.post_state_hash,
            patch_sha256,
        )
        staged_artifacts: list[StagedArtifact] = []
        published_artifacts: list[StagedArtifact] = []
        transaction_body_completed = False
        try:
            patch_staged = self._stage_artifact(
                artifact_id=_stable_id(
                    "change-patch",
                    task_id,
                    patch_sha256,
                ),
                session_id=session_id,
                task_id=task_id,
                artifact_type=ArtifactType.PATCH,
                content=capture.patch_bytes,
                redacted=False,
            )
            staged_artifacts.append(patch_staged)
            patch = patch_staged.record

            evidence_bindings: list[EvidenceBinding] = []
            for kind in ("status", "staged", "unstaged"):
                staged = self._stage_artifact(
                    artifact_id=_stable_id(
                        f"change-{kind}",
                        task_id,
                        expected_evidence[kind],
                    ),
                    session_id=session_id,
                    task_id=task_id,
                    artifact_type=ArtifactType.DIFF,
                    content=evidence[kind],
                    redacted=True,
                )
                staged_artifacts.append(staged)
                record = staged.record
                evidence_bindings.append(
                    EvidenceBinding(kind=kind, artifact_ref=_artifact_ref(record))
                )

            preimage_bindings: list[PreimageBinding] = []
            for preimage in capture.preimages:
                if sha256(preimage.content).hexdigest() != preimage.sha256:
                    raise ChangeSetIntegrityError(
                        f"captured preimage hash mismatch: {preimage.path}"
                    )
                staged = self._stage_artifact(
                    artifact_id=_stable_id(
                        "change-preimage",
                        task_id,
                        preimage.path,
                        preimage.sha256,
                    ),
                    session_id=session_id,
                    task_id=task_id,
                    artifact_type=ArtifactType.CHANGE_PREIMAGE,
                    content=preimage.content,
                    redacted=False,
                )
                staged_artifacts.append(staged)
                record = staged.record
                preimage_bindings.append(
                    PreimageBinding(
                        path=preimage.path,
                        artifact_ref=_artifact_ref(record),
                        mode=preimage.mode,
                        baseline_ignored=preimage.baseline_ignored,
                    )
                )

            document = StoredChangeSetDocument(
                manifest=capture.manifest,
                canonical_patch_ref=_artifact_ref(patch),
                evidence=tuple(evidence_bindings),
                preimages=tuple(preimage_bindings),
            )
            manifest_json = canonical_json(document).decode("utf-8")
            timestamp = utc_now_text(now)
            result = self._record_from_document(
                change_set_id=change_set_id,
                session_id=session_id,
                workflow_run_id=workflow_run_id,
                node_run_id=node_run_id,
                task_id=task_id,
                status=status,
                document=document,
                created_at=timestamp,
                updated_at=timestamp,
            )
            async with self._database.immediate_transaction() as transaction:
                await self._master_leases.assert_valid_in(
                    transaction,
                    master_lease,
                    now=now,
                )
                await self._locks.assert_valid_in(
                    transaction,
                    workspace_lease,
                    session_id=session_id,
                    now=now,
                )
                if (
                    workspace_lease.owner_kind != "agent_task"
                    or workspace_lease.owner_operation_id != task_id
                ):
                    raise ChangeSetIntegrityError(
                        "capture requires the matching AgentTask workspace lease"
                    )
                lineage = await self._load_lineage_in(transaction, task_id)
                self._validate_lineage(
                    lineage,
                    session_id=session_id,
                    workflow_run_id=workflow_run_id,
                    node_run_id=node_run_id,
                    base_commit=capture.manifest.base_commit,
                )
                existing = await transaction.fetch_one(
                    "SELECT * FROM change_sets WHERE task_id = ?",
                    (task_id,),
                )
                if existing is not None and (
                    str(existing["id"]) != change_set_id
                    or str(existing["manifest_json"]) != manifest_json
                    or str(existing["status"]) != status.value
                ):
                    raise ConcurrencyConflict("task already owns a different ChangeSet capture")
                for staged in staged_artifacts:
                    _record, created = await self._artifacts.publish_staged_in(
                        transaction,
                        staged,
                        allow_existing=True,
                    )
                    if created:
                        published_artifacts.append(staged)
                if existing is None:
                    await self._insert_capture_in(
                        transaction,
                        lineage=lineage,
                        document=document,
                        change_set_id=change_set_id,
                        task_id=task_id,
                        node_run_id=node_run_id,
                        status=status,
                        reason=reason,
                        master_lease=master_lease,
                        workspace_lease=workspace_lease,
                        timestamp=timestamp,
                        now=now,
                    )
                await self._finish_task_in(
                    transaction,
                    lineage=lineage,
                    task_id=task_id,
                    target=task_target,
                    error_code=task_error_code,
                    master_lease=master_lease,
                    workspace_lease=workspace_lease,
                    timestamp=timestamp,
                    now=now,
                )
                transaction_body_completed = True
        except BaseException as operation_error:
            if transaction_body_completed:
                operation_error.add_note(
                    "capture artifacts retained because database commit outcome is uncertain"
                )
                _LOGGER.warning(
                    "Retaining %d capture artifacts after database-boundary failure",
                    len(published_artifacts),
                )
            else:
                self._abort_staged_artifacts(staged_artifacts, operation_error)
            raise
        for staged in published_artifacts:
            self._artifacts.finalize_staged(staged)
        return result

    async def transition(
        self,
        change_set_id: str,
        *,
        expected: ChangeSetStatus,
        target: ChangeSetStatus,
        master_lease: MasterLease,
        workspace_lease: WorkspaceLease | None = None,
        reason: str | None = None,
        severity: SecuritySeverity | None = None,
        now: datetime | None = None,
    ) -> ChangeSetRecord:
        """Advance a ChangeSet with a fenced compare-and-swap mutation."""

        if target not in _TRANSITIONS.get(expected, frozenset()):
            raise ValueError(f"invalid ChangeSet transition: {expected.value} -> {target.value}")
        if severity is not None and not reason:
            raise ValueError("security event transitions require a reason")
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            await self._master_leases.assert_valid_in(transaction, master_lease, now=now)
            row = await transaction.fetch_one(
                """
                SELECT cs.*, t.node_run_id, nr.workflow_run_id,
                       wr.session_id, wr.workflow_id
                FROM change_sets cs
                JOIN tasks t ON cs.task_id = t.id
                JOIN node_runs nr ON t.node_run_id = nr.id
                JOIN workflow_runs wr ON nr.workflow_run_id = wr.id
                WHERE cs.id = ?
                """,
                (change_set_id,),
            )
            if row is None:
                raise RecordNotFound(f"ChangeSet not found: {change_set_id}")
            if workspace_lease is not None:
                await self._locks.assert_valid_in(
                    transaction,
                    workspace_lease,
                    session_id=str(row["session_id"]),
                    now=now,
                )
            changed = await transaction.execute(
                """
                UPDATE change_sets SET status = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (target.value, timestamp, change_set_id, expected.value),
            )
            if changed != 1:
                raise ConcurrencyConflict(
                    f"ChangeSet {change_set_id} did not have status {expected.value}"
                )
            await self._append_state_event_in(
                transaction,
                lineage=row,
                change_set_id=change_set_id,
                patch_sha256=str(row["patch_sha256"]),
                previous=expected,
                status=target,
                master_lease=master_lease,
                workspace_lease=workspace_lease,
                reason=reason,
                now=now,
            )
            if severity is not None:
                assert reason is not None
                await self._append_security_event_in(
                    transaction,
                    lineage=row,
                    change_set_id=change_set_id,
                    previous=expected,
                    status=target,
                    reason=reason,
                    severity=severity,
                    master_lease=master_lease,
                    workspace_lease=workspace_lease,
                    now=now,
                )
        return await self.get(change_set_id)

    async def get(self, change_set_id: str) -> ChangeSetRecord:
        """Load and cryptographically verify a persisted ChangeSet."""

        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT cs.*, t.node_run_id, nr.workflow_run_id, wr.session_id
                FROM change_sets cs
                JOIN tasks t ON cs.task_id = t.id
                JOIN node_runs nr ON t.node_run_id = nr.id
                JOIN workflow_runs wr ON nr.workflow_run_id = wr.id
                WHERE cs.id = ?
                """,
                (change_set_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            raise RecordNotFound(f"ChangeSet not found: {change_set_id}")
        return await self._verified_record(row)

    async def get_by_task(self, task_id: str) -> ChangeSetRecord:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                "SELECT id FROM change_sets WHERE task_id = ?",
                (task_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            raise RecordNotFound(f"ChangeSet for task not found: {task_id}")
        return await self.get(str(row["id"]))

    async def get_for_source_node(
        self,
        *,
        workflow_run_id: str,
        source_node_id: str,
    ) -> ChangeSetRecord:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT cs.id
                FROM node_runs nr
                JOIN tasks t ON t.node_run_id = nr.id
                JOIN change_sets cs ON cs.task_id = t.id
                WHERE nr.workflow_run_id = ? AND nr.node_id = ?
                ORDER BY nr.attempt DESC
                LIMIT 1
                """,
                (workflow_run_id, source_node_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            raise RecordNotFound(f"ChangeSet for source node not found: {source_node_id}")
        return await self.get(str(row["id"]))

    async def load_patch(self, change_set_id: str) -> tuple[ChangeSetRecord, bytes]:
        record = await self.get(change_set_id)
        _metadata, content = await self._artifacts.get_and_verify(
            record.change_set.canonical_patch_ref.artifact_id,
            expected_session_id=record.change_set.session_id,
            expected_task_id=record.change_set.task_id,
        )
        if sha256(content).hexdigest() != record.change_set.patch_sha256:
            raise ChangeSetIntegrityError("canonical patch artifact hash mismatch")
        return record, content

    async def load_preimages(self, change_set_id: str) -> tuple[FilePreimage, ...]:
        record = await self.get(change_set_id)
        preimages: list[FilePreimage] = []
        for binding in record.document.preimages:
            _metadata, content = await self._artifacts.get_and_verify(
                binding.artifact_ref.artifact_id,
                expected_session_id=record.change_set.session_id,
                expected_task_id=record.change_set.task_id,
            )
            if sha256(content).hexdigest() != binding.artifact_ref.sha256:
                raise ChangeSetIntegrityError(f"preimage artifact hash mismatch: {binding.path}")
            preimages.append(
                FilePreimage(
                    path=binding.path,
                    sha256=binding.artifact_ref.sha256,
                    size_bytes=binding.artifact_ref.size_bytes,
                    mode=binding.mode,
                    content=content,
                    baseline_ignored=binding.baseline_ignored,
                )
            )
        return tuple(preimages)

    async def _insert_capture_in(
        self,
        transaction: Transaction,
        *,
        lineage: aiosqlite.Row,
        document: StoredChangeSetDocument,
        change_set_id: str,
        task_id: str,
        node_run_id: str,
        status: ChangeSetStatus,
        reason: str | None,
        master_lease: MasterLease,
        workspace_lease: WorkspaceLease,
        timestamp: str,
        now: datetime | None,
    ) -> None:
        manifest = document.manifest
        patch_ref = document.canonical_patch_ref
        await transaction.execute(
            """
            INSERT INTO change_sets(
                id, task_id, base_commit, pre_state_hash,
                post_state_hash, patch_sha256, manifest_json,
                patch_artifact_id, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                change_set_id,
                task_id,
                manifest.base_commit,
                manifest.pre_state_hash,
                manifest.post_state_hash,
                patch_ref.sha256,
                canonical_json(document).decode("utf-8"),
                patch_ref.artifact_id,
                status.value,
                timestamp,
                timestamp,
            ),
        )
        changed = await transaction.execute(
            """
            UPDATE node_runs SET change_set_id = ?
            WHERE id = ? AND change_set_id IS NULL
            """,
            (change_set_id, node_run_id),
        )
        if changed != 1:
            raise ConcurrencyConflict("AgentTask node already references another ChangeSet")
        await self._append_state_event_in(
            transaction,
            lineage=lineage,
            change_set_id=change_set_id,
            patch_sha256=patch_ref.sha256,
            previous=None,
            status=status,
            master_lease=master_lease,
            workspace_lease=workspace_lease,
            reason=reason,
            now=now,
        )
        if status == ChangeSetStatus.ABANDONED_PARTIAL:
            await self._append_security_event_in(
                transaction,
                lineage=lineage,
                change_set_id=change_set_id,
                previous=None,
                status=status,
                reason=reason or "AgentTask failed after modifying the workspace",
                severity=SecuritySeverity.HIGH,
                master_lease=master_lease,
                workspace_lease=workspace_lease,
                now=now,
            )

    async def _finish_task_in(
        self,
        transaction: Transaction,
        *,
        lineage: aiosqlite.Row,
        task_id: str,
        target: TaskStatus,
        error_code: str | None,
        master_lease: MasterLease,
        workspace_lease: WorkspaceLease,
        timestamp: str,
        now: datetime | None,
    ) -> None:
        changed = await transaction.execute(
            """
            UPDATE tasks
            SET status = ?, finished_at = ?
            WHERE id = ? AND status = ?
            """,
            (
                target.value,
                timestamp,
                task_id,
                TaskStatus.RUNNING.value,
            ),
        )
        if changed != 1:
            raise ConcurrencyConflict("capture lost the write task terminal-state CAS")
        await self._events.append_in(
            transaction,
            session_id=str(lineage["session_id"]),
            workflow_id=str(lineage["workflow_id"]),
            workflow_run_id=str(lineage["workflow_run_id"]),
            event_type=TASK_STATE_CHANGED,
            actor_type=ActorType.MASTER,
            actor_id=master_lease.instance_id,
            payload=TaskEventPayload(
                master_fencing_token=master_lease.fencing_token,
                workspace_fencing_token=workspace_lease.fencing_token,
                workflow_run_id=str(lineage["workflow_run_id"]),
                node_run_id=str(lineage["node_run_id"]),
                task_id=task_id,
                previous_status=TaskStatus.RUNNING,
                status=target,
                error_code=error_code,
            ),
            now=now,
        )

    @staticmethod
    async def _load_lineage_in(
        transaction: Transaction,
        task_id: str,
    ) -> aiosqlite.Row:
        row = await transaction.fetch_one(
            """
            SELECT t.id AS task_id, t.node_run_id, t.base_commit,
                   t.status AS task_status, nr.node_id, nr.node_type,
                   nr.status AS node_status, nr.workflow_run_id,
                   wr.session_id, wr.workflow_id, wr.current_commit,
                   wr.status AS run_status, wr.cancel_requested_at,
                   wr.compiled_snapshot_json, wr.compiled_snapshot_hash
            FROM tasks t
            JOIN node_runs nr ON t.node_run_id = nr.id
            JOIN workflow_runs wr ON nr.workflow_run_id = wr.id
            WHERE t.id = ?
            """,
            (task_id,),
        )
        if row is None:
            raise RecordNotFound(f"task not found: {task_id}")
        return row

    @staticmethod
    def _validate_lineage(
        lineage: aiosqlite.Row,
        *,
        session_id: str,
        workflow_run_id: str,
        node_run_id: str,
        base_commit: str,
    ) -> None:
        expected = {
            "session_id": session_id,
            "workflow_run_id": workflow_run_id,
            "node_run_id": node_run_id,
            "base_commit": base_commit,
            "current_commit": base_commit,
            "node_type": "agent_task",
            "task_status": "running",
            "node_status": "running",
            "run_status": "running",
        }
        for field, value in expected.items():
            if str(lineage[field]) != value:
                raise ChangeSetIntegrityError(f"ChangeSet lineage mismatch for {field}")
        if lineage["cancel_requested_at"] is not None:
            raise ConcurrencyConflict("workflow cancellation prevents ChangeSet capture")
        try:
            graph = CompiledGraph.model_validate_json(
                str(lineage["compiled_snapshot_json"]),
                strict=True,
            )
        except ValueError as error:
            raise ChangeSetIntegrityError("compiled workflow snapshot is invalid") from error
        if sha256(canonical_json(graph)).hexdigest() != str(lineage["compiled_snapshot_hash"]):
            raise ChangeSetIntegrityError("compiled workflow snapshot hash mismatch")
        node = next(
            (item for item in graph.nodes if item.id == str(lineage["node_id"])),
            None,
        )
        if node is None or node.node_type != NodeType.AGENT_TASK or not node.requires_write:
            raise ChangeSetIntegrityError(
                "capture task is not a write AgentTask in the immutable snapshot"
            )

    async def _append_state_event_in(
        self,
        transaction: Transaction,
        *,
        lineage: aiosqlite.Row,
        change_set_id: str,
        patch_sha256: str,
        previous: ChangeSetStatus | None,
        status: ChangeSetStatus,
        master_lease: MasterLease,
        workspace_lease: WorkspaceLease | None,
        reason: str | None,
        now: datetime | None,
    ) -> None:
        await self._events.append_in(
            transaction,
            session_id=str(lineage["session_id"]),
            workflow_id=str(lineage["workflow_id"]),
            workflow_run_id=str(lineage["workflow_run_id"]),
            event_type=CHANGE_SET_STATE_CHANGED,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            payload=ChangeSetEventPayload(
                master_fencing_token=master_lease.fencing_token,
                workspace_fencing_token=(
                    workspace_lease.fencing_token if workspace_lease else None
                ),
                workflow_run_id=str(lineage["workflow_run_id"]),
                node_run_id=str(lineage["node_run_id"]),
                task_id=str(lineage["task_id"]),
                change_set_id=change_set_id,
                previous_status=previous,
                status=status,
                patch_sha256=patch_sha256,
                reason=reason,
            ),
            now=now,
        )

    @staticmethod
    async def _append_security_event_in(
        transaction: Transaction,
        *,
        lineage: aiosqlite.Row,
        change_set_id: str,
        previous: ChangeSetStatus | None,
        status: ChangeSetStatus,
        reason: str,
        severity: SecuritySeverity,
        master_lease: MasterLease,
        workspace_lease: WorkspaceLease | None,
        now: datetime | None,
    ) -> None:
        payload = SecurityChangeSetPayload(
            change_set_id=change_set_id,
            previous_status=previous,
            status=status,
            reason=reason,
            master_fencing_token=master_lease.fencing_token,
            workspace_fencing_token=(workspace_lease.fencing_token if workspace_lease else None),
        )
        await transaction.execute(
            """
            INSERT INTO security_events(
                session_id, workflow_run_id, task_id,
                event_type, severity, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(lineage["session_id"]),
                str(lineage["workflow_run_id"]),
                str(lineage["task_id"]),
                "changeset.state_rejected",
                severity.value,
                canonical_json(payload).decode("utf-8"),
                utc_now_text(now),
            ),
        )

    def _stage_artifact(
        self,
        *,
        artifact_id: str,
        session_id: str,
        task_id: str,
        artifact_type: ArtifactType,
        content: bytes,
        redacted: bool,
    ) -> StagedArtifact:
        return self._artifacts.stage(
            artifact_id=artifact_id,
            session_id=session_id,
            task_id=task_id,
            artifact_type=artifact_type,
            content=content,
            redacted=redacted,
        )

    def _abort_staged_artifacts(
        self,
        artifacts: list[StagedArtifact],
        operation_error: BaseException,
    ) -> None:
        for staged in reversed(artifacts):
            try:
                self._artifacts.abort_staged(staged)
            except Exception as cleanup_error:
                artifact_id = staged.record.artifact_id
                operation_error.add_note(
                    f"ChangeSet artifact cleanup failed for {artifact_id}: {cleanup_error!r}"
                )
                _LOGGER.exception(
                    "ChangeSet artifact cleanup failed for %s",
                    artifact_id,
                )

    async def _verified_record(self, row: aiosqlite.Row) -> ChangeSetRecord:
        try:
            document = StoredChangeSetDocument.model_validate_json(
                str(row["manifest_json"]),
                strict=True,
            )
        except Exception as error:
            raise ChangeSetIntegrityError("ChangeSet manifest is invalid") from error
        if canonical_json(document).decode("utf-8") != str(row["manifest_json"]):
            raise ChangeSetIntegrityError("ChangeSet manifest is not canonical JSON")

        manifest = document.manifest
        patch_ref = document.canonical_patch_ref
        expected_row = {
            "base_commit": manifest.base_commit,
            "pre_state_hash": manifest.pre_state_hash,
            "post_state_hash": manifest.post_state_hash,
            "patch_sha256": patch_ref.sha256,
            "patch_artifact_id": patch_ref.artifact_id,
        }
        for field, expected in expected_row.items():
            if str(row[field]) != expected:
                raise ChangeSetIntegrityError(
                    f"ChangeSet row does not match manifest field {field}"
                )

        refs = [
            patch_ref,
            *(binding.artifact_ref for binding in document.evidence),
            *(binding.artifact_ref for binding in document.preimages),
        ]
        if len(refs) != len({ref.artifact_id for ref in refs}):
            raise ChangeSetIntegrityError("ChangeSet artifact references are not unique")
        contents: dict[str, bytes] = {}
        for ref in refs:
            metadata, content = await self._artifacts.get_and_verify(
                ref.artifact_id,
                expected_session_id=str(row["session_id"]),
                expected_task_id=str(row["task_id"]),
            )
            if (
                metadata.task_id != str(row["task_id"])
                or metadata.planner_run_id is not None
                or metadata.artifact_type != ref.artifact_type.value
                or metadata.relative_path != ref.relative_path
                or metadata.sha256 != ref.sha256
                or metadata.size_bytes != ref.size_bytes
            ):
                raise ChangeSetIntegrityError(
                    f"ChangeSet artifact metadata mismatch: {ref.artifact_id}"
                )
            contents[ref.artifact_id] = content

        if patch_ref.artifact_type != ArtifactType.PATCH:
            raise ChangeSetIntegrityError("canonical patch has the wrong artifact type")
        if sha256(contents[patch_ref.artifact_id]).hexdigest() != patch_ref.sha256:
            raise ChangeSetIntegrityError("canonical patch content hash mismatch")

        evidence_by_kind = {item.kind: item for item in document.evidence}
        expected_evidence = {
            "status": manifest.status_evidence_sha256,
            "staged": manifest.staged_evidence_sha256,
            "unstaged": manifest.unstaged_evidence_sha256,
        }
        for kind, expected_hash in expected_evidence.items():
            ref = evidence_by_kind[kind].artifact_ref
            if ref.artifact_type != ArtifactType.DIFF:
                raise ChangeSetIntegrityError(f"{kind} evidence has the wrong type")
            if sha256(contents[ref.artifact_id]).hexdigest() != expected_hash:
                raise ChangeSetIntegrityError(f"{kind} evidence content hash mismatch")

        expected_preimages = {
            change.old_path or change.path
            for change in manifest.changes
            if change.action != FileAction.CREATED
        }
        actual_preimages = {item.path for item in document.preimages}
        if not expected_preimages.issubset(actual_preimages):
            raise ChangeSetIntegrityError("ChangeSet is missing a modified-path preimage")
        unexpected = actual_preimages - expected_preimages
        if any(not item.baseline_ignored for item in document.preimages if item.path in unexpected):
            raise ChangeSetIntegrityError("unexpected ChangeSet preimage is not baseline ignored")
        for binding in document.preimages:
            if binding.artifact_ref.artifact_type != ArtifactType.CHANGE_PREIMAGE:
                raise ChangeSetIntegrityError("preimage has the wrong artifact type")
            if (
                sha256(contents[binding.artifact_ref.artifact_id]).hexdigest()
                != binding.artifact_ref.sha256
            ):
                raise ChangeSetIntegrityError(f"preimage content hash mismatch: {binding.path}")

        created = [item.path for item in manifest.changes if item.action == FileAction.CREATED]
        modified = [item.path for item in manifest.changes if item.action == FileAction.MODIFIED]
        deleted = [item.path for item in manifest.changes if item.action == FileAction.DELETED]
        renamed = [item.path for item in manifest.changes if item.action == FileAction.RENAMED]
        try:
            change_set = ChangeSet(
                change_set_id=str(row["id"]),
                session_id=str(row["session_id"]),
                workflow_run_id=str(row["workflow_run_id"]),
                node_run_id=str(row["node_run_id"]),
                task_id=str(row["task_id"]),
                base_commit=manifest.base_commit,
                pre_state_hash=manifest.pre_state_hash,
                post_state_hash=manifest.post_state_hash,
                patch_sha256=patch_ref.sha256,
                status=ChangeSetStatus(str(row["status"])),
                canonical_patch_ref=patch_ref,
                evidence_refs=[item.artifact_ref for item in document.evidence],
                created_files=created,
                created_directories=list(manifest.created_directories),
                modified_files=modified,
                deleted_files=deleted,
                renamed_files=renamed,
                untracked_files=created,
                ignored_files_touched=list(manifest.ignored_files_touched),
                preimage_refs=[item.artifact_ref for item in document.preimages],
            )
        except Exception as error:
            raise ChangeSetIntegrityError("persisted ChangeSet contract is invalid") from error
        return ChangeSetRecord(
            change_set=change_set,
            document=document,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _record_from_document(
        *,
        change_set_id: str,
        session_id: str,
        workflow_run_id: str,
        node_run_id: str,
        task_id: str,
        status: ChangeSetStatus,
        document: StoredChangeSetDocument,
        created_at: str,
        updated_at: str,
    ) -> ChangeSetRecord:
        manifest = document.manifest
        patch_ref = document.canonical_patch_ref
        created = [item.path for item in manifest.changes if item.action == FileAction.CREATED]
        modified = [item.path for item in manifest.changes if item.action == FileAction.MODIFIED]
        deleted = [item.path for item in manifest.changes if item.action == FileAction.DELETED]
        renamed = [item.path for item in manifest.changes if item.action == FileAction.RENAMED]
        change_set = ChangeSet(
            change_set_id=change_set_id,
            session_id=session_id,
            workflow_run_id=workflow_run_id,
            node_run_id=node_run_id,
            task_id=task_id,
            base_commit=manifest.base_commit,
            pre_state_hash=manifest.pre_state_hash,
            post_state_hash=manifest.post_state_hash,
            patch_sha256=patch_ref.sha256,
            status=status,
            canonical_patch_ref=patch_ref,
            evidence_refs=[item.artifact_ref for item in document.evidence],
            created_files=created,
            created_directories=list(manifest.created_directories),
            modified_files=modified,
            deleted_files=deleted,
            renamed_files=renamed,
            untracked_files=created,
            ignored_files_touched=list(manifest.ignored_files_touched),
            preimage_refs=[item.artifact_ref for item in document.preimages],
        )
        return ChangeSetRecord(
            change_set=change_set,
            document=document,
            created_at=created_at,
            updated_at=updated_at,
        )


def _stable_id(prefix: str, *values: str) -> str:
    digest = sha256("\0".join(values).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"
