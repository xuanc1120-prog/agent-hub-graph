from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from storage.db import Database
from storage.errors import LeaseLost, LeaseUnavailable
from storage.leases import WorkspaceLeaseRepository
from workspace.lock_manager import LockManager, WorkspaceOwnerKind


@pytest_asyncio.fixture
async def runtime_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "agent-hub.db")
    await database.initialize()
    return database


@pytest.mark.asyncio
async def test_session_lock_facade_preserves_single_writer_and_fencing(
    runtime_database: Database,
) -> None:
    manager = LockManager(WorkspaceLeaseRepository(runtime_database))
    now = datetime.now(UTC)
    first = await manager.acquire(
        session_id="session-lock",
        owner_kind=WorkspaceOwnerKind.AGENT_TASK,
        owner_operation_id="task-one",
        owner_process_id=os.getpid(),
        ttl_seconds=10,
        now=now,
    )

    with pytest.raises(LeaseUnavailable):
        await manager.acquire(
            session_id="session-lock",
            owner_kind=WorkspaceOwnerKind.TEST,
            owner_operation_id="test-one",
            owner_process_id=os.getpid(),
            ttl_seconds=10,
            now=now + timedelta(seconds=1),
        )

    successor = await manager.acquire(
        session_id="session-lock",
        owner_kind=WorkspaceOwnerKind.RECOVERY,
        owner_operation_id="recovery-one",
        owner_process_id=os.getpid(),
        ttl_seconds=10,
        now=now + timedelta(seconds=11),
    )

    assert successor.fencing_token > first.fencing_token
    with pytest.raises(LeaseLost):
        await manager.assert_valid(
            first,
            session_id="session-lock",
            now=now + timedelta(seconds=12),
        )
    await manager.assert_valid(
        successor,
        session_id="session-lock",
        now=now + timedelta(seconds=12),
    )


@pytest.mark.asyncio
async def test_session_lock_rejects_cross_session_assertion(
    runtime_database: Database,
) -> None:
    manager = LockManager(WorkspaceLeaseRepository(runtime_database))
    lease = await manager.acquire(
        session_id="session-one",
        owner_kind=WorkspaceOwnerKind.VALIDATE,
        owner_operation_id="validate-one",
        owner_process_id=os.getpid(),
        ttl_seconds=10,
    )

    with pytest.raises(ValueError, match="does not belong"):
        await manager.assert_valid(lease, session_id="session-two")


@pytest.mark.asyncio
async def test_hold_preserves_body_error_when_release_also_fails(
    runtime_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LockManager(WorkspaceLeaseRepository(runtime_database))

    async def fail_release(lease: object) -> None:
        _ = lease
        raise RuntimeError("release failed")

    monkeypatch.setattr(manager, "release", fail_release)

    with pytest.raises(ValueError, match="body failed") as captured:
        async with manager.hold(
            session_id="session-hold",
            owner_kind=WorkspaceOwnerKind.TEST,
            owner_operation_id="test-hold",
            owner_process_id=os.getpid(),
            ttl_seconds=10,
            heartbeat_seconds=1,
        ):
            raise ValueError("body failed")

    assert any(
        "workspace lease release failed" in note
        for note in getattr(captured.value, "__notes__", ())
    )

    with pytest.raises(ValueError, match="must be positive"):
        async with manager.hold(
            session_id="session-hold",
            owner_kind=WorkspaceOwnerKind.TEST,
            owner_operation_id="test-invalid-heartbeat",
            owner_process_id=os.getpid(),
            ttl_seconds=10,
            heartbeat_seconds=0,
        ):
            pass


@pytest.mark.asyncio
async def test_hold_cancels_owner_when_heartbeat_fails(
    runtime_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LockManager(WorkspaceLeaseRepository(runtime_database))
    heartbeat_called = asyncio.Event()

    async def fail_heartbeat(
        lease: object,
        *,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> object:
        _ = lease, ttl_seconds, now
        heartbeat_called.set()
        raise LeaseLost("injected heartbeat failure")

    monkeypatch.setattr(manager, "heartbeat", fail_heartbeat)

    with pytest.raises(LeaseLost, match="heartbeat failed"):
        async with manager.hold(
            session_id="session-heartbeat-failure",
            owner_kind=WorkspaceOwnerKind.AGENT_TASK,
            owner_operation_id="task-heartbeat-failure",
            owner_process_id=os.getpid(),
            ttl_seconds=10,
            heartbeat_seconds=0.01,
        ):
            await asyncio.sleep(10)

    assert heartbeat_called.is_set()
    replacement = await manager.acquire(
        session_id="session-heartbeat-failure",
        owner_kind=WorkspaceOwnerKind.RECOVERY,
        owner_operation_id="recovery-heartbeat-failure",
        owner_process_id=os.getpid(),
        ttl_seconds=10,
    )
    await manager.release(replacement)


@pytest.mark.asyncio
async def test_hold_normal_exit_does_not_treat_heartbeat_cancellation_as_loss(
    runtime_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LockManager(WorkspaceLeaseRepository(runtime_database))
    heartbeat_started = asyncio.Event()

    async def wait_forever(
        lease: object,
        *,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> object:
        _ = ttl_seconds, now
        heartbeat_started.set()
        await asyncio.Event().wait()
        return lease

    monkeypatch.setattr(manager, "heartbeat", wait_forever)

    async with manager.hold(
        session_id="session-heartbeat-cancel",
        owner_kind=WorkspaceOwnerKind.TEST,
        owner_operation_id="test-heartbeat-cancel",
        owner_process_id=os.getpid(),
        ttl_seconds=10,
        heartbeat_seconds=0.01,
    ):
        await asyncio.wait_for(heartbeat_started.wait(), timeout=1)

    replacement = await manager.acquire(
        session_id="session-heartbeat-cancel",
        owner_kind=WorkspaceOwnerKind.RECOVERY,
        owner_operation_id="recovery-heartbeat-cancel",
        owner_process_id=os.getpid(),
        ttl_seconds=10,
    )
    await manager.release(replacement)
