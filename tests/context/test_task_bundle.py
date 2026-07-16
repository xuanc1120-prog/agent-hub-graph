"""Tests for context.task_bundle — staged publish, TTL, manifest, cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from context.task_bundle import (
    BundleManifest,
    BundleResult,
    CleanupResult,
    TaskContextBundle,
)
from protocol import ArtifactRef, ArtifactType
from storage.artifact_repository import ArtifactRepository
from storage.artifact_store import ArtifactStore
from storage.db import Database
from storage.errors import BundleError
from storage.repositories import NewSession, SessionRepository


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "data" / "agent-hub.db")
    await db.initialize()
    return db


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts", max_artifact_bytes=1024 * 1024)


@pytest_asyncio.fixture
async def repo(database: Database, store: ArtifactStore) -> ArtifactRepository:
    return ArtifactRepository(database, store)


@pytest_asyncio.fixture
async def session_repo(database: Database) -> SessionRepository:
    return SessionRepository(database)


@pytest_asyncio.fixture
async def bundle(tmp_path: Path, repo: ArtifactRepository) -> TaskContextBundle:
    return TaskContextBundle(
        repo,
        tmp_path / "bundles",
        max_bundle_bytes=1024 * 1024,
        ttl_seconds=3600,
    )


async def _create_session_and_artifact(
    repo: ArtifactRepository,
    session_repo: SessionRepository,
    session_id: str,
    artifact_id: str,
    content: bytes,
) -> None:
    """Create a session and an artifact for bundle testing."""
    await session_repo.create(
        NewSession(
            session_id=session_id,
            goal="test",
            source_repo_path=Path("/tmp/src"),
            shared_repo_path=Path(f"/tmp/shared-{session_id}"),
            base_commit="a" * 40,
            integration_branch="main",
            integration_head_commit="b" * 40,
        )
    )
    await repo.create(
        artifact_id=artifact_id,
        session_id=session_id,
        artifact_type=ArtifactType.LOG,
        content=content,
        redacted=True,
    )


# --- Materialization -------------------------------------------------------


class TestMaterialize:
    @pytest.mark.asyncio
    async def test_materialize_creates_bundle(
        self,
        bundle: TaskContextBundle,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        await _create_session_and_artifact(repo, session_repo, "sess-001", "art-001", b"hello")
        record = await repo.get("art-001")
        ref = ArtifactRef(
            artifact_id="art-001",
            artifact_type=ArtifactType.LOG,
            relative_path=record.relative_path,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
        )
        result = await bundle.materialize(
            task_id="task-001",
            session_id="sess-001",
            artifact_refs=[ref],
        )
        assert isinstance(result, BundleResult)
        assert result.bundle_dir.exists()
        assert len(result.manifest.entries) == 1
        assert result.manifest.task_id == "task-001"
        assert result.manifest.total_bytes > 0

    @pytest.mark.asyncio
    async def test_materialize_writes_manifest_json(
        self,
        bundle: TaskContextBundle,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        await _create_session_and_artifact(repo, session_repo, "sess-001", "art-m01", b"data")
        record = await repo.get("art-m01")
        ref = ArtifactRef(
            artifact_id="art-m01",
            artifact_type=ArtifactType.LOG,
            relative_path=record.relative_path,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
        )
        result = await bundle.materialize(
            task_id="task-001",
            session_id="sess-001",
            artifact_refs=[ref],
        )
        manifest_path = result.bundle_dir / "manifest.json"
        assert manifest_path.exists()
        loaded = BundleManifest.model_validate_json(manifest_path.read_bytes())
        assert loaded.task_id == "task-001"
        assert len(loaded.entries) == 1

    @pytest.mark.asyncio
    async def test_materialize_unredacted_rejected(
        self,
        bundle: TaskContextBundle,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        await session_repo.create(
            NewSession(
                session_id="sess-unred",
                goal="test",
                source_repo_path=Path("/tmp/src"),
                shared_repo_path=Path("/tmp/shared-unred"),
                base_commit="a" * 40,
                integration_branch="main",
                integration_head_commit="b" * 40,
            )
        )
        await repo.create(
            artifact_id="art-unred",
            session_id="sess-unred",
            artifact_type=ArtifactType.LOG,
            content=b"data",
            redacted=False,
        )
        record = await repo.get("art-unred")
        ref = ArtifactRef(
            artifact_id="art-unred",
            artifact_type=ArtifactType.LOG,
            relative_path=record.relative_path,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
        )
        with pytest.raises(BundleError, match="not redacted"):
            await bundle.materialize(
                task_id="task-001",
                session_id="sess-unred",
                artifact_refs=[ref],
            )

    @pytest.mark.asyncio
    async def test_materialize_cross_session_rejected(
        self,
        bundle: TaskContextBundle,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        await _create_session_and_artifact(repo, session_repo, "sess-001", "art-cross", b"data")
        record = await repo.get("art-cross")
        ref = ArtifactRef(
            artifact_id="art-cross",
            artifact_type=ArtifactType.LOG,
            relative_path=record.relative_path,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
        )
        with pytest.raises(BundleError, match="verification failed"):
            await bundle.materialize(
                task_id="task-001",
                session_id="sess-other",
                artifact_refs=[ref],
            )

    @pytest.mark.asyncio
    async def test_materialize_bundle_size_limit(
        self,
        tmp_path: Path,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        small_bundle = TaskContextBundle(
            repo,
            tmp_path / "bundles-small",
            max_bundle_bytes=10,
            ttl_seconds=3600,
        )
        await _create_session_and_artifact(repo, session_repo, "sess-001", "art-big", b"x" * 20)
        record = await repo.get("art-big")
        ref = ArtifactRef(
            artifact_id="art-big",
            artifact_type=ArtifactType.LOG,
            relative_path=record.relative_path,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
        )
        with pytest.raises(BundleError, match="size limit exceeded"):
            await small_bundle.materialize(
                task_id="task-001",
                session_id="sess-001",
                artifact_refs=[ref],
            )

    @pytest.mark.asyncio
    async def test_materialize_ref_hash_mismatch_rejected(
        self,
        bundle: TaskContextBundle,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        await _create_session_and_artifact(repo, session_repo, "sess-001", "art-hash", b"data")
        ref = ArtifactRef(
            artifact_id="art-hash",
            artifact_type=ArtifactType.LOG,
            relative_path="logs/art-hash",
            sha256="f" * 64,
            size_bytes=4,
        )
        with pytest.raises(BundleError, match="sha256 mismatch"):
            await bundle.materialize(
                task_id="task-001",
                session_id="sess-001",
                artifact_refs=[ref],
            )

    @pytest.mark.asyncio
    async def test_materialize_empty_refs(
        self,
        bundle: TaskContextBundle,
        session_repo: SessionRepository,
    ) -> None:
        await session_repo.create(
            NewSession(
                session_id="sess-empty",
                goal="test",
                source_repo_path=Path("/tmp/src"),
                shared_repo_path=Path("/tmp/shared-empty"),
                base_commit="a" * 40,
                integration_branch="main",
                integration_head_commit="b" * 40,
            )
        )
        result = await bundle.materialize(
            task_id="task-empty",
            session_id="sess-empty",
            artifact_refs=[],
        )
        assert result.manifest.entries == ()
        assert result.manifest.total_bytes == 0

    @pytest.mark.asyncio
    async def test_ttl_applied_to_expires_at(
        self,
        bundle: TaskContextBundle,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        await _create_session_and_artifact(repo, session_repo, "sess-001", "art-ttl", b"data")
        record = await repo.get("art-ttl")
        ref = ArtifactRef(
            artifact_id="art-ttl",
            artifact_type=ArtifactType.LOG,
            relative_path=record.relative_path,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
        )
        result = await bundle.materialize(
            task_id="task-ttl",
            session_id="sess-001",
            artifact_refs=[ref],
        )
        entry = result.manifest.entries[0]
        # expires_at should be in the future (now + 3600s)
        assert entry.expires_at > record.created_at


# --- Cleanup ---------------------------------------------------------------


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_removes_bundle(
        self,
        bundle: TaskContextBundle,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        await _create_session_and_artifact(repo, session_repo, "sess-001", "art-cln", b"data")
        record = await repo.get("art-cln")
        ref = ArtifactRef(
            artifact_id="art-cln",
            artifact_type=ArtifactType.LOG,
            relative_path=record.relative_path,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
        )
        result = await bundle.materialize(
            task_id="task-cln",
            session_id="sess-001",
            artifact_refs=[ref],
        )
        assert result.bundle_dir.exists()

        cleanup = await bundle.cleanup("task-cln")
        assert isinstance(cleanup, CleanupResult)
        assert cleanup.removed_files >= 1
        assert not result.bundle_dir.exists()

    @pytest.mark.asyncio
    async def test_cleanup_idempotent(self, bundle: TaskContextBundle) -> None:
        cleanup = await bundle.cleanup("nonexistent")
        assert cleanup.removed_files == 0
        assert cleanup.removed_dirs == 0
        assert cleanup.errors == []

    @pytest.mark.asyncio
    async def test_double_cleanup(
        self,
        bundle: TaskContextBundle,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        await _create_session_and_artifact(repo, session_repo, "sess-001", "art-dcl", b"data")
        record = await repo.get("art-dcl")
        ref = ArtifactRef(
            artifact_id="art-dcl",
            artifact_type=ArtifactType.LOG,
            relative_path=record.relative_path,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
        )
        await bundle.materialize(
            task_id="task-dcl",
            session_id="sess-001",
            artifact_refs=[ref],
        )
        c1 = await bundle.cleanup("task-dcl")
        assert c1.removed_files >= 1
        c2 = await bundle.cleanup("task-dcl")
        assert c2.removed_files == 0

    @pytest.mark.asyncio
    async def test_cleanup_errors_use_relative_paths(self, bundle: TaskContextBundle) -> None:
        """Cleanup errors should not leak absolute host paths."""
        cleanup = await bundle.cleanup("nonexistent")
        for err in cleanup.errors:
            assert not err.startswith("/")
            assert "C:\\" not in err


# --- Manifest structure ----------------------------------------------------


class TestManifest:
    @pytest.mark.asyncio
    async def test_manifest_hash_deterministic(
        self,
        bundle: TaskContextBundle,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        await _create_session_and_artifact(
            repo, session_repo, "sess-001", "art-hash", b"fixed content"
        )
        record = await repo.get("art-hash")
        ref = ArtifactRef(
            artifact_id="art-hash",
            artifact_type=ArtifactType.LOG,
            relative_path=record.relative_path,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
        )
        from datetime import UTC, datetime

        fixed_now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)
        r1 = await bundle.materialize(
            task_id="task-hash",
            session_id="sess-001",
            artifact_refs=[ref],
            now=fixed_now,
        )
        await bundle.cleanup("task-hash")
        r2 = await bundle.materialize(
            task_id="task-hash",
            session_id="sess-001",
            artifact_refs=[ref],
            now=fixed_now,
        )
        assert r1.manifest.bundle_sha256 == r2.manifest.bundle_sha256

    @pytest.mark.asyncio
    async def test_manifest_is_frozen(
        self,
        bundle: TaskContextBundle,
        repo: ArtifactRepository,
        session_repo: SessionRepository,
    ) -> None:
        await _create_session_and_artifact(repo, session_repo, "sess-001", "art-frozen", b"data")
        record = await repo.get("art-frozen")
        ref = ArtifactRef(
            artifact_id="art-frozen",
            artifact_type=ArtifactType.LOG,
            relative_path=record.relative_path,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
        )
        result = await bundle.materialize(
            task_id="task-frozen",
            session_id="sess-001",
            artifact_refs=[ref],
        )
        # Frozen model should reject assignment
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            result.manifest.total_bytes = 999
        # entries is a tuple (truly immutable)
        assert isinstance(result.manifest.entries, tuple)
