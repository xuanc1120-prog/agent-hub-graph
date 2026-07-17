"""Planner run persistence with fenced CAS state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from protocol import ActorType, PlannerRunStatus, PlannerType
from storage.db import Database, Transaction, utc_now_text
from storage.errors import ConcurrencyConflict, RecordNotFound
from storage.event_repository import EventRepository
from storage.leases import MasterLease, MasterLeaseRepository
from storage.repositories import NewWorkflow, WorkflowRecord, WorkflowRepository
from workflow.events import PLANNER_STATE_CHANGED, PlannerRunEventPayload

_ALLOWED_TRANSITIONS = {
    PlannerRunStatus.PENDING: {PlannerRunStatus.RUNNING, PlannerRunStatus.CANCELLED},
    PlannerRunStatus.RUNNING: {
        PlannerRunStatus.SUCCEEDED,
        PlannerRunStatus.FAILED,
        PlannerRunStatus.TIMED_OUT,
        PlannerRunStatus.CANCELLED,
        PlannerRunStatus.ORPHANED,
    },
}


@dataclass(frozen=True, slots=True)
class PlannerRunRecord:
    planner_run_id: str
    session_id: str
    planner_id: str
    planner_type: PlannerType
    status: PlannerRunStatus
    integration_base_commit: str | None
    result_workflow_id: str | None
    result_semantic_version: int | None
    error_code: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None


class PlannerRunRepository:
    def __init__(
        self,
        database: Database,
        events: EventRepository,
        leases: MasterLeaseRepository,
    ) -> None:
        self._database = database
        self._events = events
        self._leases = leases

    async def create(
        self,
        *,
        planner_run_id: str,
        session_id: str,
        planner_id: str,
        planner_type: PlannerType,
        integration_base_commit: str,
        lease: MasterLease,
        now: datetime | None = None,
    ) -> PlannerRunRecord:
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            session = await transaction.fetch_one(
                "SELECT id, status, integration_head_commit FROM sessions WHERE id = ?",
                (session_id,),
            )
            if session is None:
                raise RecordNotFound(f"session not found: {session_id}")
            if str(session["status"]) != "active":
                raise ConcurrencyConflict("planner run requires an active session")
            if str(session["integration_head_commit"]) != integration_base_commit:
                raise ConcurrencyConflict("planner base commit is stale")
            await transaction.execute(
                """
                INSERT INTO planner_runs(
                    id, session_id, planner_id, planner_type,
                    integration_base_commit, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    planner_run_id,
                    session_id,
                    planner_id,
                    planner_type.value,
                    integration_base_commit,
                    timestamp,
                ),
            )
            await self._append_event(
                transaction,
                lease,
                planner_run_id=planner_run_id,
                session_id=session_id,
                previous=None,
                target=PlannerRunStatus.PENDING,
                now=now,
            )
            row = await transaction.fetch_one(
                "SELECT * FROM planner_runs WHERE id = ?",
                (planner_run_id,),
            )
        assert row is not None
        return _to_record(row)

    async def transition(
        self,
        planner_run_id: str,
        *,
        expected: PlannerRunStatus,
        target: PlannerRunStatus,
        lease: MasterLease,
        result_workflow_id: str | None = None,
        result_semantic_version: int | None = None,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> PlannerRunRecord:
        if target not in _ALLOWED_TRANSITIONS.get(expected, set()):
            raise ValueError(f"invalid planner transition: {expected.value} -> {target.value}")
        timestamp = utc_now_text(now)
        started_at = timestamp if target == PlannerRunStatus.RUNNING else None
        finished_at = (
            timestamp
            if target not in {PlannerRunStatus.PENDING, PlannerRunStatus.RUNNING}
            else None
        )
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            changed = await transaction.execute(
                """
                UPDATE planner_runs
                SET status = ?,
                    started_at = COALESCE(started_at, ?),
                    finished_at = ?,
                    result_workflow_id = COALESCE(?, result_workflow_id),
                    result_semantic_version = COALESCE(?, result_semantic_version),
                    error_code = ?
                WHERE id = ? AND status = ?
                """,
                (
                    target.value,
                    started_at,
                    finished_at,
                    result_workflow_id,
                    result_semantic_version,
                    error_code,
                    planner_run_id,
                    expected.value,
                ),
            )
            if changed != 1:
                await _raise_conflict(transaction, planner_run_id, expected)
            row = await transaction.fetch_one(
                "SELECT * FROM planner_runs WHERE id = ?",
                (planner_run_id,),
            )
            assert row is not None
            await self._append_event(
                transaction,
                lease,
                planner_run_id=planner_run_id,
                session_id=str(row["session_id"]),
                previous=expected,
                target=target,
                error_code=error_code,
                now=now,
            )
        return _to_record(row)

    async def succeed_with_workflow(
        self,
        planner_run_id: str,
        *,
        workflow_repository: WorkflowRepository,
        workflow: NewWorkflow,
        lease: MasterLease,
        now: datetime | None = None,
    ) -> tuple[PlannerRunRecord, WorkflowRecord]:
        """Commit planner success and its result workflow as one mutation."""

        if workflow.source_planner_run_id != planner_run_id:
            raise ValueError("result workflow must reference its planner run")
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            current = await transaction.fetch_one(
                "SELECT * FROM planner_runs WHERE id = ?",
                (planner_run_id,),
            )
            if current is None:
                raise RecordNotFound(f"planner run not found: {planner_run_id}")
            if PlannerRunStatus(str(current["status"])) != PlannerRunStatus.RUNNING:
                raise ConcurrencyConflict(
                    f"planner run {planner_run_id} expected running, found {current['status']}"
                )
            if str(current["session_id"]) != workflow.session_id:
                raise ValueError("result workflow must belong to the planner session")
            session = await transaction.fetch_one(
                "SELECT status, integration_head_commit FROM sessions WHERE id = ?",
                (workflow.session_id,),
            )
            if session is None:
                raise RecordNotFound(f"session not found: {workflow.session_id}")
            if str(session["status"]) != "active":
                raise ConcurrencyConflict("planner result requires an active session")
            if str(session["integration_head_commit"]) != str(current["integration_base_commit"]):
                raise ConcurrencyConflict("planner result base commit became stale")

            created = await workflow_repository.create_in(transaction, workflow, now=now)
            changed = await transaction.execute(
                """
                UPDATE planner_runs
                SET status = 'succeeded', finished_at = ?,
                    result_workflow_id = ?, result_semantic_version = ?, error_code = NULL
                WHERE id = ? AND status = 'running'
                """,
                (
                    timestamp,
                    created.workflow_id,
                    created.semantic_version,
                    planner_run_id,
                ),
            )
            if changed != 1:
                raise ConcurrencyConflict("planner success transition lost running CAS")
            await self._append_event(
                transaction,
                lease,
                planner_run_id=planner_run_id,
                session_id=workflow.session_id,
                previous=PlannerRunStatus.RUNNING,
                target=PlannerRunStatus.SUCCEEDED,
                now=now,
            )
            row = await transaction.fetch_one(
                "SELECT * FROM planner_runs WHERE id = ?",
                (planner_run_id,),
            )
        assert row is not None
        return _to_record(row), created

    async def get(self, planner_run_id: str) -> PlannerRunRecord:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM planner_runs WHERE id = ?",
                (planner_run_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            raise RecordNotFound(f"planner run not found: {planner_run_id}")
        return _to_record(row)

    async def _append_event(
        self,
        transaction: Transaction,
        lease: MasterLease,
        *,
        planner_run_id: str,
        session_id: str,
        previous: PlannerRunStatus | None,
        target: PlannerRunStatus,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> None:
        await self._events.append_in(
            transaction,
            session_id=session_id,
            event_type=PLANNER_STATE_CHANGED,
            actor_type=ActorType.MASTER,
            actor_id=lease.instance_id,
            payload=PlannerRunEventPayload(
                master_fencing_token=lease.fencing_token,
                planner_run_id=planner_run_id,
                previous_status=previous,
                status=target,
                error_code=error_code,
            ),
            now=now,
        )


async def _raise_conflict(
    transaction: Transaction,
    planner_run_id: str,
    expected: PlannerRunStatus,
) -> None:
    row = await transaction.fetch_one(
        "SELECT status FROM planner_runs WHERE id = ?",
        (planner_run_id,),
    )
    if row is None:
        raise RecordNotFound(f"planner run not found: {planner_run_id}")
    raise ConcurrencyConflict(
        f"planner run {planner_run_id} expected {expected.value}, found {row['status']}"
    )


def _to_record(row: object) -> PlannerRunRecord:
    return PlannerRunRecord(
        planner_run_id=str(row["id"]),  # type: ignore[index]
        session_id=str(row["session_id"]),  # type: ignore[index]
        planner_id=str(row["planner_id"]),  # type: ignore[index]
        planner_type=PlannerType(str(row["planner_type"])),  # type: ignore[index]
        status=PlannerRunStatus(str(row["status"])),  # type: ignore[index]
        integration_base_commit=(
            str(row["integration_base_commit"])  # type: ignore[index]
            if row["integration_base_commit"] is not None  # type: ignore[index]
            else None
        ),
        result_workflow_id=(
            str(row["result_workflow_id"])  # type: ignore[index]
            if row["result_workflow_id"] is not None  # type: ignore[index]
            else None
        ),
        result_semantic_version=(
            int(row["result_semantic_version"])  # type: ignore[index]
            if row["result_semantic_version"] is not None  # type: ignore[index]
            else None
        ),
        error_code=(
            str(row["error_code"])  # type: ignore[index]
            if row["error_code"] is not None  # type: ignore[index]
            else None
        ),
        created_at=str(row["created_at"]),  # type: ignore[index]
        started_at=(
            str(row["started_at"])  # type: ignore[index]
            if row["started_at"] is not None  # type: ignore[index]
            else None
        ),
        finished_at=(
            str(row["finished_at"])  # type: ignore[index]
            if row["finished_at"] is not None  # type: ignore[index]
            else None
        ),
    )


__all__ = ["PlannerRunRecord", "PlannerRunRepository"]
