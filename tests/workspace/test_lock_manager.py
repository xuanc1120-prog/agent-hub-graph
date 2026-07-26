from __future__ import annotations

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
