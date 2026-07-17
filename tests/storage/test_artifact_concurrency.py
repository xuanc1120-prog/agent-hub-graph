"""Regression tests for ArtifactStore concurrency and failure modes.

These tests verify the state machine, lock behavior, and error propagation
for the staged write → publish → finalize / rollback lifecycle.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import storage.artifact_store as artifact_store_module
from storage.artifact_store import (
    _DISCARDED,
    _FINALIZED,
    _PUBLISHED,
    _ROLLED_BACK,
    _STAGED,
    ArtifactStore,
)
from storage.errors import PathEscapeError


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts", max_artifact_bytes=1024 * 1024)


# --- State machine ----------------------------------------------------------


class TestStateMachine:
    def test_publish_transitions_to_published(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-001", "log", b"data")
        assert staged._state == _STAGED
        store.publish(staged)
        assert staged._state == _PUBLISHED

    def test_rollback_from_published_transitions(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-001", "log", b"data")
        store.publish(staged)
        store.rollback_publish(staged)
        assert staged._state == _ROLLED_BACK

    def test_rollback_from_staged_transitions(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-001", "log", b"data")
        store.rollback_publish(staged)
        assert staged._state == _ROLLED_BACK
        # No file should have been created
        assert not store.exists("art-001", "log")

    def test_finalize_from_published_transitions(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-001", "log", b"data")
        store.publish(staged)
        store.finalize(staged)
        assert staged._state == _FINALIZED

    def test_discard_from_staged_transitions(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-001", "log", b"data")
        store.discard_staged(staged)
        assert staged._state == _DISCARDED

    def test_discard_from_rolled_back(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-001", "log", b"data")
        store.publish(staged)
        store.rollback_publish(staged)
        store.discard_staged(staged)
        assert staged._state == _DISCARDED

    def test_publish_rejects_non_staged(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-001", "log", b"data")
        store.publish(staged)
        with pytest.raises(PathEscapeError, match="invalid handle state"):
            store.publish(staged)

    def test_finalize_rejects_non_published(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-001", "log", b"data")
        with pytest.raises(PathEscapeError, match="invalid handle state"):
            store.finalize(staged)

    def test_rollback_rejects_finalized(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-001", "log", b"data")
        store.publish(staged)
        store.finalize(staged)
        with pytest.raises(PathEscapeError, match="invalid handle state"):
            store.rollback_publish(staged)

    def test_rollback_rejects_discarded(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-001", "log", b"data")
        store.discard_staged(staged)
        with pytest.raises(PathEscapeError, match="invalid handle state"):
            store.rollback_publish(staged)


# --- Cross-store isolation -------------------------------------------------


class TestCrossStoreIsolation:
    def test_cross_store_publish_rejected(self, tmp_path: Path) -> None:
        store_a = ArtifactStore(tmp_path / "a")
        store_b = ArtifactStore(tmp_path / "b")
        staged = store_a.write_temp("art-001", "log", b"data")
        with pytest.raises(PathEscapeError, match="not from this store"):
            store_b.publish(staged)

    def test_cross_store_rollback_rejected(self, tmp_path: Path) -> None:
        store_a = ArtifactStore(tmp_path / "a")
        store_b = ArtifactStore(tmp_path / "b")
        staged = store_a.write_temp("art-001", "log", b"data")
        store_a.publish(staged)
        with pytest.raises(PathEscapeError, match="not from this store"):
            store_b.rollback_publish(staged)

    def test_cross_store_finalize_rejected(self, tmp_path: Path) -> None:
        store_a = ArtifactStore(tmp_path / "a")
        store_b = ArtifactStore(tmp_path / "b")
        staged = store_a.write_temp("art-001", "log", b"data")
        store_a.publish(staged)
        with pytest.raises(PathEscapeError, match="not from this store"):
            store_b.finalize(staged)

    def test_cross_store_discard_rejected(self, tmp_path: Path) -> None:
        store_a = ArtifactStore(tmp_path / "a")
        store_b = ArtifactStore(tmp_path / "b")
        staged = store_a.write_temp("art-001", "log", b"data")
        with pytest.raises(PathEscapeError, match="not from this store"):
            store_b.discard_staged(staged)
        assert staged._state == _STAGED


# --- Concurrency -----------------------------------------------------------


class TestConcurrency:
    def test_concurrent_rollback_only_one_deletes(self, store: ArtifactStore) -> None:
        """Only the first rollback should delete; second sees ROLLED_BACK."""
        staged = store.write_temp("art-001", "log", b"data")
        store.publish(staged)
        assert store.exists("art-001", "log")

        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def do_rollback() -> None:
            try:
                barrier.wait(timeout=5)
                store.rollback_publish(staged)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=do_rollback)
        t2 = threading.Thread(target=do_rollback)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # Exactly one should succeed, one should fail
        assert len(errors) == 1
        assert "invalid handle state" in str(errors[0])
        assert staged._state == _ROLLED_BACK
        assert not store.exists("art-001", "log")

    def test_finalize_prevents_concurrent_rollback(self, store: ArtifactStore) -> None:
        """After finalize, concurrent rollback must be rejected."""
        staged = store.write_temp("art-001", "log", b"data")
        store.publish(staged)

        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def do_finalize() -> None:
            barrier.wait(timeout=5)
            store.finalize(staged)

        def do_rollback() -> None:
            try:
                barrier.wait(timeout=5)
                # Small delay to let finalize win
                time.sleep(0.01)
                store.rollback_publish(staged)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=do_finalize)
        t2 = threading.Thread(target=do_rollback)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert staged._state == _FINALIZED
        assert len(errors) == 1
        assert "invalid handle state" in str(errors[0])
        assert store.exists("art-001", "log")

    def test_staged_rollback_does_not_delete_existing(self, store: ArtifactStore) -> None:
        """Rollback on a STAGED handle for an existing artifact ID
        must not delete the existing file."""
        # Publish first artifact
        staged1 = store.write_temp("art-001", "log", b"original")
        store.publish(staged1)
        store.finalize(staged1)
        assert store.exists("art-001", "log")

        # Create a second staged handle for the same ID
        staged2 = store.write_temp("art-001", "log", b"new")
        # Rollback the second handle (never published)
        store.rollback_publish(staged2)
        assert staged2._state == _ROLLED_BACK
        # Original must survive
        assert store.exists("art-001", "log")
        assert store.read_bytes("art-001", "log") == b"original"

    def test_publish_rejects_already_consumed_handle(self, store: ArtifactStore) -> None:
        """Re-publishing a finalized handle is rejected."""
        staged = store.write_temp("art-001", "log", b"data")
        store.publish(staged)
        store.finalize(staged)
        with pytest.raises(PathEscapeError, match="invalid handle state"):
            store.publish(staged)

    def test_publish_and_discard_have_one_winner(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-publish-discard", "log", b"data")
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def publish() -> None:
            try:
                barrier.wait(timeout=5)
                store.publish(staged)
            except Exception as exc:
                errors.append(exc)

        def discard() -> None:
            try:
                barrier.wait(timeout=5)
                store.discard_staged(staged)
            except Exception as exc:
                errors.append(exc)

        publish_thread = threading.Thread(target=publish)
        discard_thread = threading.Thread(target=discard)
        publish_thread.start()
        discard_thread.start()
        publish_thread.join(timeout=10)
        discard_thread.join(timeout=10)

        assert not publish_thread.is_alive()
        assert not discard_thread.is_alive()
        assert len(errors) == 1
        if staged._state == _PUBLISHED:
            assert store.read_bytes("art-publish-discard", "log") == b"data"
        else:
            assert staged._state == _DISCARDED
            assert not store.exists("art-publish-discard", "log")


# --- Tamper detection ------------------------------------------------------


class TestTamperDetection:
    def test_publish_detects_size_tamper(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-001", "log", b"original")
        # Restore write permission before tampering
        from storage.artifact_store import _restore_write_permissions

        _restore_write_permissions(staged.tmp_path)
        staged.tmp_path.write_bytes(b"much longer tampered content")
        with pytest.raises(PathEscapeError, match="temp size changed"):
            store.publish(staged)

    def test_publish_detects_hash_tamper(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-001", "log", b"original")
        # Restore write permission before tampering
        from storage.artifact_store import _restore_write_permissions

        _restore_write_permissions(staged.tmp_path)
        # Write same-length but different content
        staged.tmp_path.write_bytes(b"TAMPARED")
        with pytest.raises(PathEscapeError, match="temp hash changed"):
            store.publish(staged)

    def test_publish_detects_check_link_tamper(
        self,
        store: ArtifactStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        staged = store.write_temp("art-check-link", "log", b"original")
        original_link = artifact_store_module.os.link

        def tampering_link(source: str, target: str) -> None:
            Path(source).write_bytes(b"tampered-after-check")
            original_link(source, target)

        monkeypatch.setattr(artifact_store_module.os, "link", tampering_link)

        with pytest.raises(PathEscapeError, match="published size changed"):
            store.publish(staged)
        assert staged._state == _ROLLED_BACK
        assert not store.exists("art-check-link", "log")
        store.discard_staged(staged)

    def test_publish_detects_post_link_regular_replacement(
        self,
        store: ArtifactStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        staged = store.write_temp("art-link-replace", "log", b"original")
        original_link = artifact_store_module.os.link

        def replacing_link(source: str, target: str) -> None:
            original_link(source, target)
            target_path = Path(target)
            target_path.unlink()
            target_path.write_bytes(b"regular-replacement")

        monkeypatch.setattr(artifact_store_module.os, "link", replacing_link)

        with pytest.raises(PathEscapeError, match="no longer references"):
            store.publish(staged)
        assert staged._state == _ROLLED_BACK
        assert not store.exists("art-link-replace", "log")
        store.discard_staged(staged)


# --- Token validation ------------------------------------------------------


class TestTokenValidation:
    def test_cannot_construct_without_token(self) -> None:
        from storage.artifact_store import TempWriteResult

        with pytest.raises(ValueError, match="only be created"):
            TempWriteResult(
                tmp_path=Path("/tmp/fake"),
                artifact_id="x",
                artifact_type="log",
                sha256="a" * 64,
                size_bytes=0,
            )

    def test_handle_fields_immutable(self, store: ArtifactStore) -> None:
        staged = store.write_temp("art-001", "log", b"data")
        with pytest.raises(AttributeError, match="immutable"):
            staged.sha256 = "changed"  # type: ignore[misc]
        with pytest.raises(AttributeError, match="immutable"):
            staged.size_bytes = 999  # type: ignore[misc]
        with pytest.raises(AttributeError, match="immutable"):
            staged.tmp_path = Path("/other")  # type: ignore[misc]
