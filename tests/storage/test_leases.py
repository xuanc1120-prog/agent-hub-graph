from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from storage.db import Database
from storage.errors import LeaseLost, LeaseUnavailable
from storage.leases import MasterLease, MasterLeaseRepository, WorkspaceLeaseRepository

BASE = datetime(2026, 7, 12, 1, 0, 0, tzinfo=UTC)


async def test_concurrent_master_acquire_has_one_winner(database: Database) -> None:
    repository = MasterLeaseRepository(database)

    async def acquire(instance_id: str, process_id: int) -> MasterLease | LeaseUnavailable:
        try:
            return await repository.acquire(
                instance_id=instance_id,
                process_id=process_id,
                ttl_seconds=15,
                now=BASE,
            )
        except LeaseUnavailable as error:
            return error

    results = await asyncio.gather(acquire("master-1", 101), acquire("master-2", 202))
    leases = [result for result in results if not isinstance(result, LeaseUnavailable)]
    failures = [result for result in results if isinstance(result, LeaseUnavailable)]
    assert len(leases) == 1
    assert len(failures) == 1
    assert leases[0].fencing_token == 1


async def test_master_lease_is_singleton_and_token_never_rewinds(database: Database) -> None:
    repository = MasterLeaseRepository(database)
    first = await repository.acquire(
        instance_id="master-1", process_id=101, ttl_seconds=15, now=BASE
    )
    assert first.fencing_token == 1

    with pytest.raises(LeaseUnavailable):
        await repository.acquire(
            instance_id="master-2",
            process_id=202,
            ttl_seconds=15,
            now=BASE + timedelta(seconds=1),
        )

    heartbeat = await repository.heartbeat(first, ttl_seconds=15, now=BASE + timedelta(seconds=2))
    assert heartbeat.fencing_token == first.fencing_token
    await repository.release(heartbeat, now=BASE + timedelta(seconds=3))

    second = await repository.acquire(
        instance_id="master-2",
        process_id=202,
        ttl_seconds=15,
        now=BASE + timedelta(seconds=3),
    )
    assert second.fencing_token == 2
    with pytest.raises(LeaseLost):
        await repository.assert_valid(first, now=BASE + timedelta(seconds=4))
    with pytest.raises(LeaseLost):
        await repository.heartbeat(first, ttl_seconds=15, now=BASE + timedelta(seconds=4))


async def test_expired_master_takeover_fences_old_owner(database: Database) -> None:
    repository = MasterLeaseRepository(database)
    old = await repository.acquire(
        instance_id="master-old", process_id=101, ttl_seconds=5, now=BASE
    )
    current = await repository.acquire(
        instance_id="master-new",
        process_id=202,
        ttl_seconds=5,
        now=BASE + timedelta(seconds=6),
    )
    assert current.fencing_token == old.fencing_token + 1
    with pytest.raises(LeaseLost):
        await repository.release(old, now=BASE + timedelta(seconds=7))
    await repository.assert_valid(current, now=BASE + timedelta(seconds=7))


async def test_stale_master_cannot_commit_a_guarded_mutation(database: Database) -> None:
    repository = MasterLeaseRepository(database)
    old = await repository.acquire(
        instance_id="master-old", process_id=101, ttl_seconds=5, now=BASE
    )
    await repository.acquire(
        instance_id="master-new",
        process_id=202,
        ttl_seconds=5,
        now=BASE + timedelta(seconds=6),
    )
    async with database.connection() as connection:
        await connection.execute("CREATE TABLE lease_guard_probe(value TEXT NOT NULL)")

    with pytest.raises(LeaseLost):
        async with database.immediate_transaction() as transaction:
            await transaction.execute("INSERT INTO lease_guard_probe(value) VALUES ('stale')")
            await repository.assert_valid_in(
                transaction,
                old,
                now=BASE + timedelta(seconds=7),
            )

    async with database.connection() as connection:
        cursor = await connection.execute("SELECT COUNT(*) FROM lease_guard_probe")
        row = await cursor.fetchone()
        await cursor.close()
    assert row[0] == 0


async def test_workspace_release_retains_row_and_increments_fence(database: Database) -> None:
    repository = WorkspaceLeaseRepository(database)
    first = await repository.acquire(
        resource_key="session:1",
        owner_kind="agent_task",
        owner_operation_id="task-1",
        owner_process_id=101,
        ttl_seconds=30,
        now=BASE,
    )
    with pytest.raises(LeaseUnavailable):
        await repository.acquire(
            resource_key="session:1",
            owner_kind="test",
            owner_operation_id="test-1",
            owner_process_id=202,
            ttl_seconds=30,
            now=BASE + timedelta(seconds=1),
        )

    await repository.release(first, now=BASE + timedelta(seconds=2))
    second = await repository.acquire(
        resource_key="session:1",
        owner_kind="test",
        owner_operation_id="test-1",
        owner_process_id=202,
        ttl_seconds=30,
        now=BASE + timedelta(seconds=2),
    )
    assert second.fencing_token == 2
    with pytest.raises(LeaseLost):
        await repository.assert_valid(first, now=BASE + timedelta(seconds=3))

    async with database.connection() as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*), MAX(fencing_token) FROM file_locks WHERE resource_key = 'session:1'"
        )
        row = await cursor.fetchone()
        await cursor.close()
    assert tuple(row) == (1, 2)


async def test_expired_workspace_takeover_rejects_stale_heartbeat(database: Database) -> None:
    repository = WorkspaceLeaseRepository(database)
    old = await repository.acquire(
        resource_key="session:2",
        owner_kind="agent_task",
        owner_operation_id="task-old",
        owner_process_id=101,
        ttl_seconds=5,
        now=BASE,
    )
    current = await repository.acquire(
        resource_key="session:2",
        owner_kind="recovery",
        owner_operation_id="recovery-1",
        owner_process_id=202,
        ttl_seconds=5,
        now=BASE + timedelta(seconds=6),
    )
    assert current.fencing_token == 2
    with pytest.raises(LeaseLost):
        await repository.heartbeat(old, ttl_seconds=5, now=BASE + timedelta(seconds=7))
    await repository.assert_valid(current, now=BASE + timedelta(seconds=7))


async def test_workspace_fenced_operation_excludes_expired_takeover(
    database: Database,
) -> None:
    owner = WorkspaceLeaseRepository(database)
    contender = WorkspaceLeaseRepository(database)
    lease = await owner.acquire(
        resource_key="session:fenced",
        owner_kind="agent_task",
        owner_operation_id="task-fenced",
        owner_process_id=101,
        ttl_seconds=5,
        now=BASE,
    )
    takeover_started = threading.Event()
    takeover_finished = threading.Event()
    outcomes: list[object] = []

    def takeover_thread() -> None:
        async def acquire() -> object:
            takeover_started.set()
            return await contender.acquire(
                resource_key="session:fenced",
                owner_kind="recovery",
                owner_operation_id="recovery-fenced",
                owner_process_id=202,
                ttl_seconds=30,
                now=BASE + timedelta(seconds=6),
            )

        try:
            outcomes.append(asyncio.run(acquire()))
        except BaseException as error:
            outcomes.append(error)
        finally:
            takeover_finished.set()

    thread = threading.Thread(target=takeover_thread, daemon=True)

    def fenced_operation() -> str:
        thread.start()
        assert takeover_started.wait(timeout=1)
        time.sleep(0.2)
        assert not takeover_finished.is_set()
        return "captured"

    result = await owner.run_fenced(
        lease,
        fenced_operation,
        now=BASE + timedelta(seconds=1),
    )
    await asyncio.to_thread(thread.join, 5)

    assert result == "captured"
    assert not thread.is_alive()
    assert len(outcomes) == 1
    replacement = outcomes[0]
    assert not isinstance(replacement, BaseException)
    assert replacement.fencing_token == lease.fencing_token + 1


async def test_stale_workspace_lease_cannot_enter_fenced_operation(
    database: Database,
) -> None:
    repository = WorkspaceLeaseRepository(database)
    old = await repository.acquire(
        resource_key="session:stale-fenced",
        owner_kind="agent_task",
        owner_operation_id="task-stale-fenced",
        owner_process_id=101,
        ttl_seconds=5,
        now=BASE,
    )
    await repository.acquire(
        resource_key="session:stale-fenced",
        owner_kind="recovery",
        owner_operation_id="recovery-stale-fenced",
        owner_process_id=202,
        ttl_seconds=30,
        now=BASE + timedelta(seconds=6),
    )
    operation_called = False

    def stale_operation() -> None:
        nonlocal operation_called
        operation_called = True

    with pytest.raises(LeaseLost):
        await repository.run_fenced(
            old,
            stale_operation,
            now=BASE + timedelta(seconds=7),
        )

    assert operation_called is False
