"""Tests for storage.artifact_repository — server-side metadata, ownership, quota."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from protocol import ArtifactType
from storage.artifact_repository import ArtifactRepository
from storage.artifact_store import ArtifactStore
from storage.db import Database
from storage.errors import (
    ArtifactNotFound,
    PathEscapeError,
    QuotaExceeded,
    RecordNotFound,
)
from storage.repositories import NewSession, SessionRepository


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "data" / "agent-hub.db")
    await db.initialize()
    return db


@pytest_asyncio.fixture
async def session_repo(database: Database) -> SessionRepository:
    return SessionRepository(database)


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts", max_artifact_bytes=1024 * 1024)


@pytest_asyncio.fixture
async def repo(database: Database, store: ArtifactStore) -> ArtifactRepository:
    return ArtifactRepository(
        database,
        store,
        max_artifact_bytes=1024 * 1024,
        max_session_artifact_bytes=10 * 1024 * 1024,
    )


async def _create_session(session_repo: SessionRepository, session_id: str = "sess-001") -> None:
    await session_repo.create(
        NewSession(
            session_id=session_id,
            goal="test goal",
            source_repo_path=Path("/tmp/source"),
            shared_repo_path=Path(f"/tmp/shared-{session_id}"),
            base_commit="a" * 40,
            integration_branch="main",
            integration_head_commit="b" * 40,
        )
    )


# --- Basic CRUD (server-side metadata) -------------------------------------


class TestArtifactCRUD:
    @pytest.mark.asyncio
    async def test_create_and_get(
        self,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        await _create_session(session_repo)
        record = await repo.create(
            artifact_id="art-001",
            session_id="sess-001",
            artifact_type=ArtifactType.LOG,
            content=b"hello world",
        )
        assert record.artifact_id == "art-001"
        assert record.session_id == "sess-001"
        assert record.sha256 != "a" * 64  # Server-computed, not caller-provided
        assert record.size_bytes == 11
        assert not record.relative_path.startswith("/")

        fetched = await repo.get("art-001")
        assert fetched.sha256 == record.sha256
        assert fetched.size_bytes == record.size_bytes

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, repo: ArtifactRepository) -> None:
        with pytest.raises(ArtifactNotFound):
            await repo.get("nonexistent")

    @pytest.mark.asyncio
    async def test_list_by_session(
        self,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        await _create_session(session_repo)
        for i in range(3):
            await repo.create(
                artifact_id=f"art-list-{i}",
                session_id="sess-001",
                artifact_type=ArtifactType.LOG,
                content=b"x" * 10,
            )
        records = await repo.list_by_session("sess-001")
        assert len(records) == 3

    @pytest.mark.asyncio
    async def test_session_total_bytes(
        self,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        await _create_session(session_repo)
        await repo.create(
            artifact_id="art-t1",
            session_id="sess-001",
            artifact_type=ArtifactType.LOG,
            content=b"a" * 100,
        )
        await repo.create(
            artifact_id="art-t2",
            session_id="sess-001",
            artifact_type=ArtifactType.DIFF,
            content=b"b" * 200,
        )
        assert await repo.session_total_bytes("sess-001") == 300

    @pytest.mark.asyncio
    async def test_delete(
        self,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        await _create_session(session_repo)
        await repo.create(
            artifact_id="art-del",
            session_id="sess-001",
            artifact_type=ArtifactType.LOG,
            content=b"hello",
        )
        await repo.delete("art-del")
        with pytest.raises(ArtifactNotFound):
            await repo.get("art-del")


# --- Immutable (no overwrite) ----------------------------------------------


class TestImmutable:
    @pytest.mark.asyncio
    async def test_duplicate_rejected_preserves_original(
        self,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        await _create_session(session_repo)
        r1 = await repo.create(
            artifact_id="art-dup",
            session_id="sess-001",
            artifact_type=ArtifactType.LOG,
            content=b"first",
        )
        with pytest.raises(PathEscapeError, match="already exists"):
            await repo.create(
                artifact_id="art-dup",
                session_id="sess-001",
                artifact_type=ArtifactType.LOG,
                content=b"second",
            )
        # Original preserved
        r2 = await repo.get("art-dup")
        assert r2.sha256 == r1.sha256
        assert r2.size_bytes == 5


# --- Quota enforcement -----------------------------------------------------


class TestQuota:
    @pytest.mark.asyncio
    async def test_session_quota_exceeded(
        self,
        database: Database,
        store: ArtifactStore,
        session_repo: SessionRepository,
    ) -> None:
        small_repo = ArtifactRepository(
            database,
            store,
            max_artifact_bytes=1024 * 1024,
            max_session_artifact_bytes=50,
        )
        await _create_session(session_repo)
        with pytest.raises(QuotaExceeded, match=r"session.*quota exceeded"):
            await small_repo.create(
                artifact_id="art-q01",
                session_id="sess-001",
                artifact_type=ArtifactType.LOG,
                content=b"x" * 60,
            )

    @pytest.mark.asyncio
    async def test_per_artifact_quota(
        self,
        database: Database,
        store: ArtifactStore,
        session_repo: SessionRepository,
    ) -> None:
        tiny_repo = ArtifactRepository(
            database,
            store,
            max_artifact_bytes=10,
            max_session_artifact_bytes=1024 * 1024,
        )
        await _create_session(session_repo)
        with pytest.raises(QuotaExceeded, match="per-artifact limit"):
            await tiny_repo.create(
                artifact_id="art-q02",
                session_id="sess-001",
                artifact_type=ArtifactType.LOG,
                content=b"x" * 20,
            )


# --- Ownership checks ------------------------------------------------------


class TestOwnership:
    @pytest.mark.asyncio
    async def test_create_without_session_fails(self, repo: ArtifactRepository) -> None:
        with pytest.raises(RecordNotFound, match="session not found"):
            await repo.create(
                artifact_id="art-no-sess",
                session_id="nonexistent",
                artifact_type=ArtifactType.LOG,
                content=b"hello",
            )

    @pytest.mark.asyncio
    async def test_owner_exclusivity(
        self, repo: ArtifactRepository, session_repo: SessionRepository
    ) -> None:
        await _create_session(session_repo)
        with pytest.raises(ValueError, match="mutually exclusive"):
            await repo.create(
                artifact_id="art-conflict",
                session_id="sess-001",
                artifact_type=ArtifactType.LOG,
                content=b"x",
                task_id="t1",
                planner_run_id="p1",
            )

    @pytest.mark.asyncio
    async def test_get_and_verify_cross_session_rejected(
        self,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        await _create_session(session_repo, "sess-a")
        await _create_session(session_repo, "sess-b")
        await repo.create(
            artifact_id="art-cross",
            session_id="sess-a",
            artifact_type=ArtifactType.LOG,
            content=b"hello",
        )
        with pytest.raises(ArtifactNotFound, match="does not belong"):
            await repo.get_and_verify("art-cross", expected_session_id="sess-b")


# --- Hash / size re-verification -------------------------------------------


class TestReverification:
    @pytest.mark.asyncio
    async def test_get_and_verify_hash_matches(
        self,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        await _create_session(session_repo)
        content = b"verify me"
        await repo.create(
            artifact_id="art-verify",
            session_id="sess-001",
            artifact_type=ArtifactType.LOG,
            content=content,
        )
        rec, read_content = await repo.get_and_verify("art-verify", expected_session_id="sess-001")
        assert read_content == content
        from hashlib import sha256

        assert sha256(read_content).hexdigest() == rec.sha256

    @pytest.mark.asyncio
    async def test_relative_path_stored(
        self,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        await _create_session(session_repo)
        record = await repo.create(
            artifact_id="art-rel",
            session_id="sess-001",
            artifact_type=ArtifactType.LOG,
            content=b"data",
        )
        assert not record.relative_path.startswith("/")
        assert not record.relative_path.startswith("C:")
