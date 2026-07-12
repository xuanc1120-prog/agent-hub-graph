from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path

import pytest

from storage.db import Database
from storage.errors import IdempotencyConflict
from storage.idempotency_repository import (
    IdempotencyRepository,
    IdempotencyResult,
    MutationContext,
    StoredResponse,
)
from storage.repositories import NewSession, SessionRepository

REQUEST_HASH = sha256(b"request-v1").hexdigest()
OTHER_HASH = sha256(b"request-v2").hexdigest()


async def _create_effects_table(database: Database) -> None:
    async with database.connection() as connection:
        await connection.execute(
            """
            CREATE TABLE mutation_effects(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                value TEXT NOT NULL
            )
            """
        )


async def _effect_count(database: Database) -> int:
    async with database.connection() as connection:
        cursor = await connection.execute("SELECT COUNT(*) FROM mutation_effects")
        row = await cursor.fetchone()
        await cursor.close()
    assert row is not None
    return int(row[0])


async def test_same_key_replays_without_reexecuting_mutation(database: Database) -> None:
    await _create_effects_table(database)
    repository = IdempotencyRepository(database)
    calls = 0

    async def mutation(connection: MutationContext) -> StoredResponse:
        nonlocal calls
        calls += 1
        await connection.execute("INSERT INTO mutation_effects(value) VALUES ('once')")
        return StoredResponse(201, '{"id":"created"}')

    first = await repository.execute(
        actor_scope="user:1",
        operation_scope="POST:/sessions",
        idempotency_key="key-1",
        request_sha256=REQUEST_HASH,
        mutation=mutation,
        ttl_seconds=60,
    )
    replay = await repository.execute(
        actor_scope="user:1",
        operation_scope="POST:/sessions",
        idempotency_key="key-1",
        request_sha256=REQUEST_HASH,
        mutation=mutation,
        ttl_seconds=60,
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.response == first.response
    assert calls == 1
    assert await _effect_count(database) == 1


async def test_same_key_with_different_hash_conflicts(database: Database) -> None:
    repository = IdempotencyRepository(database)

    async def mutation(_: MutationContext) -> StoredResponse:
        return StoredResponse(200, "{}")

    await repository.execute(
        actor_scope="user:1",
        operation_scope="PUT:/workflow/1",
        idempotency_key="key-1",
        request_sha256=REQUEST_HASH,
        mutation=mutation,
        ttl_seconds=60,
    )
    with pytest.raises(IdempotencyConflict):
        await repository.execute(
            actor_scope="user:1",
            operation_scope="PUT:/workflow/1",
            idempotency_key="key-1",
            request_sha256=OTHER_HASH,
            mutation=mutation,
            ttl_seconds=60,
        )


async def test_mutation_and_key_roll_back_together(database: Database) -> None:
    await _create_effects_table(database)
    repository = IdempotencyRepository(database)

    async def failing(connection: MutationContext) -> StoredResponse:
        await connection.execute("INSERT INTO mutation_effects(value) VALUES ('rollback')")
        raise RuntimeError("mutation failed")

    with pytest.raises(RuntimeError):
        await repository.execute(
            actor_scope="user:1",
            operation_scope="POST:/run",
            idempotency_key="key-rollback",
            request_sha256=REQUEST_HASH,
            mutation=failing,
            ttl_seconds=60,
        )

    assert await _effect_count(database) == 0
    async with database.connection() as connection:
        cursor = await connection.execute("SELECT COUNT(*) FROM idempotency_keys")
        row = await cursor.fetchone()
        await cursor.close()
    assert row is not None and row[0] == 0


async def test_concurrent_same_key_executes_exactly_once(database: Database) -> None:
    await _create_effects_table(database)
    repository = IdempotencyRepository(database)
    calls = 0

    async def mutation(connection: MutationContext) -> StoredResponse:
        nonlocal calls
        calls += 1
        await connection.execute("INSERT INTO mutation_effects(value) VALUES ('concurrent')")
        await asyncio.sleep(0.02)
        return StoredResponse(202, '{"queued":true}')

    async def execute() -> IdempotencyResult:
        return await repository.execute(
            actor_scope="user:1",
            operation_scope="POST:/plan",
            idempotency_key="key-concurrent",
            request_sha256=REQUEST_HASH,
            mutation=mutation,
            ttl_seconds=60,
        )

    results = await asyncio.gather(execute(), execute())
    assert sorted(result.replayed for result in results) == [False, True]
    assert calls == 1
    assert await _effect_count(database) == 1


async def test_typed_repository_composes_inside_idempotency_transaction(
    database: Database, tmp_path: Path
) -> None:
    idempotency = IdempotencyRepository(database)
    sessions = SessionRepository(database)
    value = NewSession(
        session_id="session-idempotent",
        goal="Create once",
        source_repo_path=tmp_path / "source",
        shared_repo_path=tmp_path / "shared",
        base_commit="a" * 40,
        integration_branch="integration",
        integration_head_commit="a" * 40,
    )

    async def mutation(transaction: MutationContext) -> StoredResponse:
        created = await sessions.create_in(transaction, value)
        return StoredResponse(201, f'{{"session_id":"{created.session_id}"}}')

    first = await idempotency.execute(
        actor_scope="user:1",
        operation_scope="POST:/sessions",
        idempotency_key="typed-session",
        request_sha256=REQUEST_HASH,
        mutation=mutation,
        ttl_seconds=60,
    )
    replay = await idempotency.execute(
        actor_scope="user:1",
        operation_scope="POST:/sessions",
        idempotency_key="typed-session",
        request_sha256=REQUEST_HASH,
        mutation=mutation,
        ttl_seconds=60,
    )

    assert first.replayed is False and replay.replayed is True
    assert (await sessions.get("session-idempotent")).goal == "Create once"
