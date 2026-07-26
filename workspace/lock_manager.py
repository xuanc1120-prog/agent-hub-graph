"""Session-scoped facade over SQLite workspace leases and fencing tokens."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import TypeAdapter

from protocol import EntityId
from storage.db import Transaction
from storage.leases import WorkspaceLease, WorkspaceLeaseRepository

_ENTITY_ID = TypeAdapter(EntityId)


class WorkspaceOwnerKind(StrEnum):
    AGENT_TASK = "agent_task"
    TEST = "test"
    MERGE = "merge"
    CONTEXT = "context"
    PLANNER_CONTEXT = "planner_context"
    VALIDATE = "validate"
    RUN = "run"
    RECOVERY = "recovery"


class LockManager:
    """Enforce the one-write-lease-per-session resource naming contract."""

    def __init__(self, repository: WorkspaceLeaseRepository) -> None:
        self._repository = repository

    async def acquire(
        self,
        *,
        session_id: str,
        owner_kind: WorkspaceOwnerKind,
        owner_operation_id: str,
        owner_process_id: int,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> WorkspaceLease:
        resolved_session = _ENTITY_ID.validate_python(session_id)
        return await self._repository.acquire(
            resource_key=self.resource_key(resolved_session),
            owner_kind=owner_kind.value,
            owner_operation_id=_ENTITY_ID.validate_python(owner_operation_id),
            owner_process_id=owner_process_id,
            ttl_seconds=ttl_seconds,
            now=now,
        )

    async def heartbeat(
        self,
        lease: WorkspaceLease,
        *,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> WorkspaceLease:
        return await self._repository.heartbeat(
            lease,
            ttl_seconds=ttl_seconds,
            now=now,
        )

    async def release(
        self,
        lease: WorkspaceLease,
        *,
        now: datetime | None = None,
    ) -> None:
        await self._repository.release(lease, now=now)

    async def assert_valid(
        self,
        lease: WorkspaceLease,
        *,
        session_id: str,
        now: datetime | None = None,
    ) -> None:
        self._assert_session(lease, session_id)
        await self._repository.assert_valid(lease, now=now)

    async def assert_valid_in(
        self,
        transaction: Transaction,
        lease: WorkspaceLease,
        *,
        session_id: str,
        now: datetime | None = None,
    ) -> None:
        self._assert_session(lease, session_id)
        await self._repository.assert_valid_in(transaction, lease, now=now)

    @staticmethod
    def resource_key(session_id: str) -> str:
        return f"session:{_ENTITY_ID.validate_python(session_id)}:integration"

    @classmethod
    def _assert_session(cls, lease: WorkspaceLease, session_id: str) -> None:
        if lease.resource_key != cls.resource_key(session_id):
            raise ValueError("workspace lease does not belong to the requested session")


__all__ = ["LockManager", "WorkspaceOwnerKind"]
