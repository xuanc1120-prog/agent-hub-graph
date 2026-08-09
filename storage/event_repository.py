"""Typed event repository with atomic run-sequence allocation.

Run events acquire their ``run_seq`` from the ``workflow_runs.next_event_seq``
counter inside the same ``BEGIN IMMEDIATE`` transaction that inserts the event
row.  ``MAX(seq)+1`` is never used — the counter is monotonically incremented
and the UNIQUE ``(workflow_run_id, run_seq)`` constraint provides a hard
backstop.

Non-run events must carry ``workflow_run_id = NULL`` and ``run_seq = NULL``.
Run events must carry all three of ``workflow_id``, ``workflow_run_id`` and
``run_seq``.  Ownership of session, workflow and run is validated inside the
write transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import aiosqlite
from pydantic import TypeAdapter

from protocol import ActorType, EntityId, StrictModel, canonical_json
from storage.db import Database, Transaction, utc_now_text
from storage.errors import EventPayloadError, RecordNotFound
from storage.event_registry import EventRegistry

# Bounded read limits
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 500

_ENTITY_ID = TypeAdapter(EntityId)


def _entity_id(value: str) -> str:
    return _ENTITY_ID.validate_python(value)


@dataclass(frozen=True, slots=True)
class EventRecord:
    """Read-only projection of a persisted event row."""

    event_id: int
    session_id: str
    workflow_id: str | None
    workflow_run_id: str | None
    run_seq: int | None
    event_type: str
    actor_type: str
    actor_id: str | None
    payload_json: str
    created_at: str


def _validate_limit(value: int, label: str) -> int:
    if value < 1:
        raise ValueError(f"{label} must be >= 1, got {value}")
    return min(value, _MAX_LIMIT)


class EventRepository:
    """CRUD operations for typed events in SQLite.

    Parameters
    ----------
    database:
        Shared :class:`Database` handle.
    registry:
        Typed event registry used to validate payloads before persistence.
    """

    def __init__(self, database: Database, registry: EventRegistry) -> None:
        self._database = database
        self._registry = registry

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def append(
        self,
        *,
        session_id: str,
        event_type: str,
        actor_type: ActorType,
        actor_id: str | None,
        payload: StrictModel,
        workflow_id: str | None = None,
        workflow_run_id: str | None = None,
        now: datetime | None = None,
    ) -> EventRecord:
        """Append a typed event.

        For **run events** (``workflow_run_id`` is set):
        - ``workflow_id`` and ``workflow_run_id`` are required.
        - ``run_seq`` is atomically allocated from
          ``workflow_runs.next_event_seq`` inside the same ``BEGIN IMMEDIATE``
          transaction.
        - Session/workflow/run ownership is verified inside the transaction.

        For **non-run events**:
        - ``workflow_run_id`` and ``run_seq`` must both be ``None``.
        - ``workflow_id`` may be set (workflow-level event) or ``None``.

        Raises :class:`EventPayloadError` if the payload fails registry
        validation or exceeds the canonical JSON size limit.
        """
        async with self._database.immediate_transaction() as transaction:
            return await self.append_in(
                transaction,
                session_id=session_id,
                event_type=event_type,
                actor_type=actor_type,
                actor_id=actor_id,
                payload=payload,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                now=now,
            )

    async def append_in(
        self,
        transaction: Transaction,
        *,
        session_id: str,
        event_type: str,
        actor_type: ActorType,
        actor_id: str | None,
        payload: StrictModel,
        workflow_id: str | None = None,
        workflow_run_id: str | None = None,
        now: datetime | None = None,
    ) -> EventRecord:
        """Append an event inside a caller-owned mutation transaction."""
        resolved_session = _entity_id(session_id)
        resolved_actor = _entity_id(actor_id) if actor_id else None
        resolved_wf = _entity_id(workflow_id) if workflow_id else None
        resolved_run = _entity_id(workflow_run_id) if workflow_run_id else None

        # Validate payload against registry (typed, not dict)
        validated = self._registry.validate_payload(event_type, payload)
        canonical_bytes = canonical_json(validated)
        payload_json = canonical_bytes.decode("utf-8")

        timestamp = utc_now_text(now)
        is_run_event = resolved_run is not None

        # Pre-validate field consistency
        if is_run_event:
            if resolved_wf is None:
                raise EventPayloadError("run event must carry workflow_id")
        else:
            # Non-run: workflow_run_id and run_seq must both be None
            # workflow_id is allowed (workflow-level event)
            pass

        session_row = await transaction.fetch_one(
            "SELECT id FROM sessions WHERE id = ?",
            (resolved_session,),
        )
        if session_row is None:
            raise RecordNotFound(f"session not found: {resolved_session}")

        run_seq: int | None = None

        if is_run_event:
            assert resolved_wf is not None
            assert resolved_run is not None

            wf_row = await transaction.fetch_one(
                "SELECT session_id FROM workflows WHERE id = ?",
                (resolved_wf,),
            )
            if wf_row is None:
                raise RecordNotFound(f"workflow not found: {resolved_wf}")
            if str(wf_row["session_id"]) != resolved_session:
                raise EventPayloadError(
                    f"workflow {resolved_wf} does not belong to session {resolved_session}"
                )

            run_row = await transaction.fetch_one(
                """
                SELECT session_id, workflow_id, next_event_seq
                FROM workflow_runs WHERE id = ?
                """,
                (resolved_run,),
            )
            if run_row is None:
                raise RecordNotFound(f"workflow run not found: {resolved_run}")
            if str(run_row["session_id"]) != resolved_session:
                raise EventPayloadError(
                    f"workflow run {resolved_run} does not belong to session {resolved_session}"
                )
            if str(run_row["workflow_id"]) != resolved_wf:
                raise EventPayloadError(
                    f"workflow run {resolved_run} does not belong to workflow {resolved_wf}"
                )

            run_seq = int(run_row["next_event_seq"])
            changed = await transaction.execute(
                """
                UPDATE workflow_runs
                SET next_event_seq = next_event_seq + 1
                WHERE id = ? AND next_event_seq = ?
                """,
                (resolved_run, run_seq),
            )
            if changed != 1:
                raise EventPayloadError(
                    f"failed to allocate run_seq for run "
                    f"{resolved_run}; concurrent allocation detected"
                )

        await transaction.execute(
            """
            INSERT INTO events(
                session_id, workflow_id, workflow_run_id, run_seq,
                event_type, actor_type, actor_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resolved_session,
                resolved_wf,
                resolved_run,
                run_seq,
                event_type,
                actor_type.value,
                resolved_actor,
                payload_json,
                timestamp,
            ),
        )

        row = await transaction.fetch_one("SELECT last_insert_rowid() AS id")
        event_id = int(row["id"]) if row else 0

        return EventRecord(
            event_id=event_id,
            session_id=resolved_session,
            workflow_id=resolved_wf,
            workflow_run_id=resolved_run,
            run_seq=run_seq,
            event_type=event_type,
            actor_type=actor_type.value,
            actor_id=resolved_actor,
            payload_json=payload_json,
            created_at=timestamp,
        )

    async def append_security_event(
        self,
        *,
        session_id: str,
        workflow_run_id: str | None,
        task_id: str | None,
        event_type: str,
        severity: Literal["info", "warning", "high", "critical"],
        payload: StrictModel,
        now: datetime | None = None,
    ) -> None:
        """Persist a typed security audit payload for a recovery mutation."""

        payload_json = canonical_json(payload).decode("utf-8")
        if len(event_type) < 1 or len(event_type) > 128:
            raise ValueError("security event type must contain 1..128 characters")
        if len(payload_json) > 65_536:
            raise ValueError("security event payload is too large")
        async with self._database.immediate_transaction() as transaction:
            session = await transaction.fetch_one(
                "SELECT id FROM sessions WHERE id = ?",
                (session_id,),
            )
            if session is None:
                raise RecordNotFound(f"session not found: {session_id}")
            if workflow_run_id is not None:
                run = await transaction.fetch_one(
                    "SELECT session_id FROM workflow_runs WHERE id = ?",
                    (workflow_run_id,),
                )
                if run is None or str(run["session_id"]) != session_id:
                    raise RecordNotFound(f"workflow run not found: {workflow_run_id}")
            if task_id is not None:
                task = await transaction.fetch_one(
                    """
                    SELECT wr.session_id FROM tasks t
                    JOIN node_runs nr ON nr.id = t.node_run_id
                    JOIN workflow_runs wr ON wr.id = nr.workflow_run_id
                    WHERE t.id = ?
                    """,
                    (task_id,),
                )
                if task is None or str(task["session_id"]) != session_id:
                    raise RecordNotFound(f"task not found: {task_id}")
            await transaction.execute(
                """
                INSERT INTO security_events(
                    session_id, workflow_run_id, task_id,
                    event_type, severity, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    workflow_run_id,
                    task_id,
                    event_type,
                    severity,
                    payload_json,
                    utc_now_text(now),
                ),
            )

    # ------------------------------------------------------------------
    # Read path (all reads validate through registry)
    # ------------------------------------------------------------------

    async def get_by_id(self, event_id: int) -> EventRecord:
        """Fetch a single event and validate payload through registry."""
        async with self._database.connection() as conn:
            cursor = await conn.execute("SELECT * FROM events WHERE id = ?", (event_id,))
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            raise RecordNotFound(f"event not found: {event_id}")
        record = self._to_record(row)
        self._registry.validate_payload_json(record.event_type, record.payload_json)
        return record

    async def list_by_session(
        self,
        session_id: str,
        *,
        after_event_id: int = 0,
        limit: int = _DEFAULT_LIMIT,
    ) -> list[EventRecord]:
        """Read events for a session in ID order.

        Parameters
        ----------
        session_id:
            The session to query.
        after_event_id:
            Return only events with ``id > after_event_id``.
        limit:
            Maximum records to return (1..500).
        """
        _entity_id(session_id)
        limit = _validate_limit(limit, "limit")
        async with self._database.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM events
                WHERE session_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (session_id, after_event_id, limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._to_validated_record(r) for r in rows]

    async def list_by_run(
        self,
        workflow_run_id: str,
        *,
        after_run_seq: int = 0,
        limit: int = _DEFAULT_LIMIT,
    ) -> list[EventRecord]:
        """Read run events in sequence order.

        Parameters
        ----------
        workflow_run_id:
            The workflow run to query.
        after_run_seq:
            Return only events with ``run_seq > after_run_seq``.
        limit:
            Maximum records to return (1..500).
        """
        _entity_id(workflow_run_id)
        limit = _validate_limit(limit, "limit")
        async with self._database.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM events
                WHERE workflow_run_id = ? AND run_seq > ?
                ORDER BY run_seq ASC
                LIMIT ?
                """,
                (workflow_run_id, after_run_seq, limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._to_validated_record(r) for r in rows]

    async def list_all_by_session(
        self,
        session_id: str,
        *,
        after_event_id: int = 0,
        page_size: int = _MAX_LIMIT,
    ) -> list[EventRecord]:
        """Replay every session event after a stable event-id cursor."""

        page_size = _validate_limit(page_size, "page_size")
        cursor = after_event_id
        result: list[EventRecord] = []
        while True:
            page = await self.list_by_session(
                session_id,
                after_event_id=cursor,
                limit=page_size,
            )
            result.extend(page)
            if len(page) < page_size:
                return result
            cursor = page[-1].event_id

    async def list_all_by_run(
        self,
        workflow_run_id: str,
        *,
        after_run_seq: int = 0,
        page_size: int = _MAX_LIMIT,
    ) -> list[EventRecord]:
        """Replay every run event after a stable run-sequence cursor."""

        page_size = _validate_limit(page_size, "page_size")
        cursor = after_run_seq
        result: list[EventRecord] = []
        while True:
            page = await self.list_by_run(
                workflow_run_id,
                after_run_seq=cursor,
                limit=page_size,
            )
            result.extend(page)
            if len(page) < page_size:
                return result
            next_cursor = page[-1].run_seq
            if next_cursor is None:
                raise EventPayloadError("run event is missing its run_seq")
            cursor = next_cursor

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _to_validated_record(self, row: aiosqlite.Row) -> EventRecord:
        """Convert a DB row to EventRecord with payload revalidation."""
        record = self._to_record(row)
        self._registry.validate_payload_json(record.event_type, record.payload_json)
        return record

    @staticmethod
    def _to_record(row: aiosqlite.Row) -> EventRecord:
        return EventRecord(
            event_id=int(row["id"]),
            session_id=str(row["session_id"]),
            workflow_id=(str(row["workflow_id"]) if row["workflow_id"] is not None else None),
            workflow_run_id=(
                str(row["workflow_run_id"]) if row["workflow_run_id"] is not None else None
            ),
            run_seq=(int(row["run_seq"]) if row["run_seq"] is not None else None),
            event_type=str(row["event_type"]),
            actor_type=str(row["actor_type"]),
            actor_id=(str(row["actor_id"]) if row["actor_id"] is not None else None),
            payload_json=str(row["payload_json"]),
            created_at=str(row["created_at"]),
        )
