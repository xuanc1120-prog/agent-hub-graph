"""Session-scoped facade over SQLite workspace leases and fencing tokens."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TypeVar

from pydantic import TypeAdapter

from protocol import EntityId
from storage.db import Transaction
from storage.errors import LeaseLost
from storage.leases import WorkspaceLease, WorkspaceLeaseRepository

_ENTITY_ID = TypeAdapter(EntityId)

_T = TypeVar("_T")


class WorkspaceOwnerKind(StrEnum):
    AGENT_TASK = "agent_task"
    TEST = "test"
    MERGE = "merge"
    CONTEXT = "context"
    PLANNER_CONTEXT = "planner_context"
    VALIDATE = "validate"
    RUN = "run"
    RECOVERY = "recovery"


@dataclass(slots=True)
class HeldWorkspaceLease:
    """A live lease plus any asynchronous heartbeat failure."""

    lease: WorkspaceLease
    heartbeat_error: BaseException | None = None

    def assert_healthy(self) -> None:
        if self.heartbeat_error is not None:
            raise LeaseLost("workspace lease heartbeat failed") from self.heartbeat_error


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

    async def run_fenced(
        self,
        held: HeldWorkspaceLease,
        *,
        session_id: str,
        ttl_seconds: int,
        operation: Callable[[], _T],
        now: datetime | None = None,
    ) -> _T:
        """Run synchronous workspace I/O while preventing lease takeover."""

        held.assert_healthy()
        self._assert_session(held.lease, session_id)
        result, renewed = await self._repository.run_fenced(
            held.lease,
            operation,
            ttl_seconds=ttl_seconds,
            now=now,
        )
        held.lease = renewed
        held.assert_healthy()
        return result

    @asynccontextmanager
    async def hold(
        self,
        *,
        session_id: str,
        owner_kind: WorkspaceOwnerKind,
        owner_operation_id: str,
        owner_process_id: int,
        ttl_seconds: int,
        heartbeat_seconds: float | None = None,
    ) -> AsyncIterator[HeldWorkspaceLease]:
        """Acquire, renew, validate, and release one workspace lease."""

        interval = max(1.0, ttl_seconds / 3) if heartbeat_seconds is None else heartbeat_seconds
        if interval <= 0 or interval >= ttl_seconds:
            raise ValueError("heartbeat_seconds must be positive and less than ttl_seconds")
        lease = await self.acquire(
            session_id=session_id,
            owner_kind=owner_kind,
            owner_operation_id=owner_operation_id,
            owner_process_id=owner_process_id,
            ttl_seconds=ttl_seconds,
        )
        held = HeldWorkspaceLease(lease=lease)
        owner_task = asyncio.current_task()

        async def heartbeat_loop() -> None:
            while True:
                await asyncio.sleep(interval)
                try:
                    held.lease = await self.heartbeat(
                        held.lease,
                        ttl_seconds=ttl_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    held.heartbeat_error = error
                    if owner_task is not None and not owner_task.done():
                        owner_task.cancel()
                    return

        heartbeat = asyncio.create_task(
            heartbeat_loop(),
            name=f"workspace-heartbeat:{owner_operation_id}",
        )
        body_error: BaseException | None = None
        try:
            yield held
            held.assert_healthy()
            await self.assert_valid(held.lease, session_id=session_id)
        except asyncio.CancelledError as error:
            if held.heartbeat_error is not None:
                lease_error = LeaseLost("workspace lease heartbeat failed")
                body_error = lease_error
                raise lease_error from held.heartbeat_error
            body_error = error
            raise
        except BaseException as error:
            body_error = error
            raise
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            late_error: BaseException | None = None
            if body_error is None:
                try:
                    held.assert_healthy()
                except BaseException as error:
                    late_error = error
            try:
                await self.release(held.lease)
            except BaseException as release_error:
                primary_error = body_error or late_error
                if primary_error is None:
                    raise
                primary_error.add_note(f"workspace lease release failed: {release_error!r}")
            if late_error is not None:
                raise late_error

    @staticmethod
    def resource_key(session_id: str) -> str:
        return f"session:{_ENTITY_ID.validate_python(session_id)}:integration"

    @classmethod
    def _assert_session(cls, lease: WorkspaceLease, session_id: str) -> None:
        if lease.resource_key != cls.resource_key(session_id):
            raise ValueError("workspace lease does not belong to the requested session")


__all__ = ["HeldWorkspaceLease", "LockManager", "WorkspaceOwnerKind"]
