"""Tests for storage.artifact_store — staged write, no-overwrite, containment."""

from __future__ import annotations

import os
import time
from hashlib import sha256
from pathlib import Path

import pytest

from storage.artifact_store import ArtifactStore
from storage.errors import PathEscapeError, QuotaExceeded


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts", max_artifact_bytes=1024 * 1024)


def _exists(store: ArtifactStore, staged) -> bool:
    return store.exists(staged.artifact_id, staged.artifact_type)


def _read(store: ArtifactStore, staged) -> bytes:
    return store.read_bytes(staged.artifact_id, staged.artifact_type)


# --- Path validation -------------------------------------------------------


class TestPathValidation:
    def test_traversal_rejected(self, store: ArtifactStore) -> None:
        with pytest.raises(PathEscapeError):
            store.resolve("../escape", "log")

    def test_invalid_entity_id_rejected(self, store: ArtifactStore) -> None:
        with pytest.raises(PathEscapeError, match="invalid artifact_id"):
            store.resolve("", "log")

    def test_invalid_artifact_type_rejected(self, store: ArtifactStore) -> None:
        with pytest.raises(PathEscapeError, match="invalid artifact_type"):
            store.resolve("art-001", "")

    def test_valid_path_resolves(self, store: ArtifactStore) -> None:
        path = store.resolve("art-001", "log")
        assert path.name == "art-001"
        assert "log" in str(path)

    def test_windows_absolute_rejected(self, store: ArtifactStore) -> None:
        with pytest.raises(PathEscapeError):
            store.resolve("C:\\Windows\\system32", "log")


# --- Staged write ----------------------------------------------------------


class TestStagedWrite:
    def test_write_temp_correct_hash_size(self, store: ArtifactStore) -> None:
        content = b"hello world"
        staged = store.write_temp("art-001", "log", content)
        assert staged.size_bytes == len(content)
        assert staged.sha256 == sha256(content).hexdigest()
        assert staged.tmp_path.exists()
        assert not _exists(store, staged)

    def test_write_temp_respects_max_size(self, store: ArtifactStore) -> None:
        big = b"x" * (1024 * 1024 + 1)
        with pytest.raises(QuotaExceeded, match="exceeds limit"):
            store.write_temp("art-big", "log", big)

    def test_write_temp_empty_content(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-empty", "log", b"")
        assert staged.size_bytes == 0
        assert staged.tmp_path.exists()


# --- No-overwrite publish --------------------------------------------------


class TestPublish:
    def test_publish_creates_final(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-001", "log", b"data")
        store.publish(staged)
        assert _exists(store, staged)
        assert _read(store, staged) == b"data"
        assert not staged.tmp_path.exists()

    def test_publish_rejects_duplicate(self, store: ArtifactStore) -> None:
        staged1 = store.write_temp("art-dup", "log", b"first")
        store.publish(staged1)
        assert _read(store, staged1) == b"first"

        staged2 = store.write_temp("art-dup", "log", b"second")
        with pytest.raises(PathEscapeError, match="already exists"):
            store.publish(staged2)
        assert _read(store, staged1) == b"first"

    def test_publish_cleans_temp_on_duplicate(self, store: ArtifactStore) -> None:
        staged1 = store.write_temp("art-dup2", "log", b"first")
        store.publish(staged1)
        staged2 = store.write_temp("art-dup2", "log", b"second")
        with pytest.raises(PathEscapeError):
            store.publish(staged2)
        assert not staged2.tmp_path.exists()


# --- Rollback --------------------------------------------------------------


class TestRollback:
    def test_rollback_removes_published(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-rb", "log", b"data")
        store.publish(staged)
        assert _exists(store, staged)
        store.rollback_publish(staged)
        assert not _exists(store, staged)

    def test_rollback_noop_if_missing(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-rb2", "log", b"data")
        store.rollback_publish(staged)


# --- Read verification -----------------------------------------------------


class TestReadVerification:
    def test_read_bytes_matches_written(self, store: ArtifactStore) -> None:
        content = b"read verification content"
        staged = store.write_temp("art-r01", "log", content)
        store.publish(staged)
        assert store.read_bytes("art-r01", "log") == content

    def test_read_missing_raises(self, store: ArtifactStore) -> None:
        with pytest.raises(PathEscapeError, match="missing"):
            store.read_bytes("nonexistent", "log")

    def test_exists_check(self, store: ArtifactStore) -> None:
        assert not store.exists("art-ex", "log")
        staged = store.write_temp("art-ex", "log", b"x")
        store.publish(staged)
        assert store.exists("art-ex", "log")


# --- Cleanup (TTL) ---------------------------------------------------------


class TestCleanup:
    def test_cleanup_removes_old_orphans(self, store: ArtifactStore) -> None:
        type_dir = store.base_dir / "log"
        type_dir.mkdir(parents=True, exist_ok=True)
        tmp = type_dir / ".artifact-orphan.tmp"
        tmp.write_bytes(b"orphan")
        old_time = time.time() - 3600
        os.utime(tmp, (old_time, old_time))
        removed = store.cleanup_temp("log", ttl_seconds=60)
        assert removed == 1
        assert not tmp.exists()

    def test_cleanup_preserves_active_temps(self, store: ArtifactStore) -> None:
        type_dir = store.base_dir / "log"
        type_dir.mkdir(parents=True, exist_ok=True)
        tmp = type_dir / ".artifact-active.tmp"
        tmp.write_bytes(b"active")
        removed = store.cleanup_temp("log", ttl_seconds=3600)
        assert removed == 0
        assert tmp.exists()

    def test_cleanup_no_orphans(self, store: ArtifactStore) -> None:
        assert store.cleanup_temp("log", ttl_seconds=1) == 0

    def test_delete_existing(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-d01", "log", b"data")
        store.publish(staged)
        assert store.delete("art-d01", "log") is True
        assert not store.exists("art-d01", "log")

    def test_delete_nonexistent(self, store: ArtifactStore) -> None:
        assert store.delete("nonexistent", "log") is False


# --- Permissions (platform-specific) ---------------------------------------


class TestPermissions:
    @pytest.mark.skipif(os.name != "nt", reason="Windows-specific test")
    def test_windows_permissions_set(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-perm", "log", b"data")
        store.publish(staged)
        assert _exists(store, staged)
        assert _read(store, staged) == b"data"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX-specific test")
    def test_posix_permissions_set(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-perm", "log", b"data")
        store.publish(staged)
        final = store.resolve(staged.artifact_id, staged.artifact_type)
        mode = final.stat().st_mode
        assert mode & 0o600
        assert not (mode & 0o077)
