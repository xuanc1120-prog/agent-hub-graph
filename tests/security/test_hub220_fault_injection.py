"""HUB-220 Security regression and fault injection tests.

Phase A: deterministic fault injection against HUB-200 production invariants.
Uses real WorkspaceTransaction, SecureWorkspaceRoot, LockManager,
ArtifactRepository, ArtifactStore, SafeTestRunner, and secret_policy.

Test matrix: tests/fixtures/hub220/security_fault_matrix.md
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import time as _time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch as mock_patch

import psutil
import pytest
import pytest_asyncio

from protocol import ArtifactType
from security.command_guard import CommandGuard
from security.test_runner import TestRunner as SafeTestRunner
from security.test_runner import _redact_output
from storage.artifact_repository import ArtifactRepository
from storage.artifact_store import ArtifactStore
from storage.db import Database, Transaction
from storage.errors import (
    LeaseLost,
    LeaseUnavailable,
    PathEscapeError,
    QuotaExceeded,
    RecordNotFound,
)
from storage.leases import MasterLeaseRepository, WorkspaceLeaseRepository
from storage.repositories import NewSession, SessionRepository
from workspace.change_set import FileAction
from workspace.git_manager import GitManager
from workspace.lock_manager import HeldWorkspaceLease, LockManager, WorkspaceOwnerKind
from workspace.secure_file import SecureFileError, SecureWorkspaceRoot
from workspace.transaction import WorkspaceNotClean, WorkspaceTransaction

_WIN32 = sys.platform == "win32"

# --- Capability probes (unique temp paths, always cleaned up) ---

_HAS_SYMLINK_PRIV = False
if _WIN32:
    _probe_dir = Path(os.environ["TEMP"]) / f"hub220-probe-{os.getpid()}"
    try:
        _probe_dir.mkdir(exist_ok=True)
        _probe = _probe_dir / "target"
        _probe.write_text("x")
        _link = _probe_dir / "link"
        os.symlink(str(_probe), str(_link))
        _HAS_SYMLINK_PRIV = True
    except OSError:
        pass
    finally:
        try:
            for f in _probe_dir.iterdir():
                f.unlink()
            _probe_dir.rmdir()
        except OSError:
            pass

_CAN_HARDLINK = True
if _WIN32:
    _hl_probe_dir = Path(os.environ["TEMP"]) / f"hub220-hl-probe-{os.getpid()}"
    try:
        _hl_probe_dir.mkdir(exist_ok=True)
        _hl_src = _hl_probe_dir / "src"
        _hl_src.write_text("x")
        _hl_dst = _hl_probe_dir / "dst"
        os.link(str(_hl_src), str(_hl_dst))
    except OSError:
        _CAN_HARDLINK = False
    finally:
        try:
            for f in _hl_probe_dir.iterdir():
                f.unlink()
            _hl_probe_dir.rmdir()
        except OSError:
            pass

_needs_symlink = pytest.mark.skipif(
    _WIN32 and not _HAS_SYMLINK_PRIV,
    reason="Windows requires SeCreateSymbolicLinkPrivilege",
)
_needs_hardlink = pytest.mark.skipif(
    _WIN32 and not _CAN_HARDLINK,
    reason="os.link unavailable on this filesystem",
)

# ---------------------------------------------------------------------------
# Git helper with isolated identity
# ---------------------------------------------------------------------------

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "HUB220 Test",
    "GIT_AUTHOR_EMAIL": "hub220@test.local",
    "GIT_COMMITTER_NAME": "HUB220 Test",
    "GIT_COMMITTER_EMAIL": "hub220@test.local",
}


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
        env=_GIT_ENV,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "data" / "agent-hub.db")
    await db.initialize()
    return db


@pytest_asyncio.fixture
async def session_repo(database: Database) -> SessionRepository:
    return SessionRepository(database)


@pytest_asyncio.fixture
async def artifact_store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts", max_artifact_bytes=1024 * 1024)


@pytest_asyncio.fixture
async def artifact_repo(
    database: Database,
    artifact_store: ArtifactStore,
) -> ArtifactRepository:
    return ArtifactRepository(
        database,
        artifact_store,
        max_artifact_bytes=1024 * 1024,
        max_session_artifact_bytes=10 * 1024 * 1024,
    )


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "src").mkdir()
    (repo / "src" / "example.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    return repo


@pytest.fixture
def workspace_repo(
    source_repo: Path,
    tmp_path: Path,
) -> tuple[GitManager, Path, str, str]:
    manager = GitManager(tmp_path / "git-profile")
    source = manager.inspect_source_repository(source_repo)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    repo = workspace_root / "session-hub220" / "repo"
    state = manager.create_session_repository(
        source=source, destination=repo, session_id="session-hub220"
    )
    return manager, repo, state.commit, state.branch


BASE = datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC)


async def _create_session(
    sr: SessionRepository,
    session_id: str = "sess-hub220",
) -> None:
    await sr.create(
        NewSession(
            session_id=session_id,
            goal="HUB-220 fault injection",
            source_repo_path=Path("/tmp/source"),
            shared_repo_path=Path(f"/tmp/shared-{session_id}"),
            base_commit="a" * 40,
            integration_branch="main",
            integration_head_commit="b" * 40,
        )
    )


def _make_txn(
    manager: GitManager,
    repo: Path,
    commit: str,
    branch: str,
    tmp_path: Path,
) -> WorkspaceTransaction:
    return WorkspaceTransaction(
        manager,
        repo,
        base_commit=commit,
        expected_branch=branch,
        temp_directory=tmp_path / "txn-temp",
    )


# ===================================================================
# 1. Path identity -- SecureWorkspaceRoot
# ===================================================================


class TestSecureRootPathIdentity:
    """SecureWorkspaceRoot must reject or detect symlink/junction/reparse."""

    @_needs_symlink
    def test_root_rejects_symlink(
        self,
        source_repo: Path,
        tmp_path: Path,
    ) -> None:
        link = tmp_path / "fake-root"
        os.symlink(str(source_repo), str(link))
        with pytest.raises((SecureFileError, OSError)):
            SecureWorkspaceRoot(link)

    def test_root_detects_identity_change(
        self,
        source_repo: Path,
        tmp_path: Path,
    ) -> None:
        root = SecureWorkspaceRoot(source_repo)
        root.assert_root_identity()
        backup = tmp_path / "backup"
        source_repo.rename(backup)
        replacement = Path(str(source_repo))
        replacement.mkdir()
        (replacement / "evil.txt").write_text("hijacked")
        with pytest.raises(SecureFileError):
            root.assert_root_identity()
        root.close()

    def test_root_identity_switch_between_operations(
        self,
        source_repo: Path,
        tmp_path: Path,
    ) -> None:
        """Parent identity changes between assert_root_identity and open_binary."""
        root = SecureWorkspaceRoot(source_repo)
        root.assert_root_identity()
        backup = tmp_path / "backup"
        source_repo.rename(backup)
        attacker = Path(str(source_repo))
        attacker.mkdir()
        (attacker / "src").mkdir()
        (attacker / "src" / "example.py").write_text("TAMPERED\n", encoding="utf-8")
        with pytest.raises((SecureFileError, OSError)), root.open_binary("src/example.py"):
            pass
        root.close()

    def test_operations_rejected_after_close(
        self,
        source_repo: Path,
    ) -> None:
        root = SecureWorkspaceRoot(source_repo)
        root.close()
        with pytest.raises(SecureFileError, match="closed"):
            root.assert_root_identity()

    @_needs_symlink
    def test_open_binary_rejects_symlink_in_subdirectory(
        self,
        source_repo: Path,
    ) -> None:
        root = SecureWorkspaceRoot(source_repo)
        link = source_repo / "src" / "link.py"
        os.symlink(str(source_repo / "src" / "example.py"), str(link))
        with pytest.raises((SecureFileError, OSError)), root.open_binary("src/link.py"):
            pass
        root.close()

    @_needs_hardlink
    def test_open_binary_rejects_hardlink(
        self,
        source_repo: Path,
    ) -> None:
        target = source_repo / "src" / "example.py"
        hardlink = source_repo / "src" / "hard.py"
        os.link(str(target), str(hardlink))
        root = SecureWorkspaceRoot(source_repo)
        with pytest.raises(SecureFileError, match="link"), root.open_binary("src/hard.py"):
            pass
        root.close()


# ===================================================================
# 2. Recovery -- WorkspaceTransaction capture_and_restore
# ===================================================================


class TestWorkspaceRecoveryFailClosed:
    """WorkspaceTransaction must restore repo to clean state after capture."""

    def test_staged_modification_restored(
        self,
        workspace_repo: tuple[GitManager, Path, str, str],
        tmp_path: Path,
    ) -> None:
        manager, repo, commit, branch = workspace_repo
        txn = _make_txn(manager, repo, commit, branch, tmp_path)
        txn.begin()
        target = repo / "src" / "example.py"
        original = target.read_bytes()
        target.write_text("CORRUPTED\n", encoding="utf-8")
        _git(repo, "add", "src/example.py")
        result = txn.capture_and_restore()
        assert (FileAction.MODIFIED, "src/example.py") in {
            (c.action, c.path) for c in result.manifest.changes
        }
        assert target.read_bytes() == original
        assert manager.state(repo).dirty is False

    def test_new_file_removed_after_capture(
        self,
        workspace_repo: tuple[GitManager, Path, str, str],
        tmp_path: Path,
    ) -> None:
        manager, repo, commit, branch = workspace_repo
        txn = _make_txn(manager, repo, commit, branch, tmp_path)
        txn.begin()
        new = repo / "src" / "new.py"
        new.write_text("created = True\n", encoding="utf-8")
        result = txn.capture_and_restore()
        assert (FileAction.CREATED, "src/new.py") in {
            (c.action, c.path) for c in result.manifest.changes
        }
        assert not new.exists()
        assert manager.state(repo).dirty is False

    def test_deleted_file_restored(
        self,
        workspace_repo: tuple[GitManager, Path, str, str],
        tmp_path: Path,
    ) -> None:
        manager, repo, commit, branch = workspace_repo
        txn = _make_txn(manager, repo, commit, branch, tmp_path)
        txn.begin()
        target = repo / "src" / "example.py"
        original = target.read_bytes()
        target.unlink()
        result = txn.capture_and_restore()
        assert (FileAction.DELETED, "src/example.py") in {
            (c.action, c.path) for c in result.manifest.changes
        }
        assert target.exists()
        assert target.read_bytes() == original
        assert manager.state(repo).dirty is False

    def test_rename_restored(
        self,
        workspace_repo: tuple[GitManager, Path, str, str],
        tmp_path: Path,
    ) -> None:
        manager, repo, commit, branch = workspace_repo
        txn = _make_txn(manager, repo, commit, branch, tmp_path)
        txn.begin()
        old = repo / "src" / "example.py"
        new = repo / "src" / "renamed.py"
        old.rename(new)
        result = txn.capture_and_restore()
        actions = {(c.action, c.path) for c in result.manifest.changes}
        assert any(a in (FileAction.RENAMED, FileAction.DELETED) for a, _ in actions)
        assert old.exists()
        assert not new.exists()
        assert manager.state(repo).dirty is False

    def test_mixed_changes_restored_clean(
        self,
        workspace_repo: tuple[GitManager, Path, str, str],
        tmp_path: Path,
    ) -> None:
        manager, repo, commit, branch = workspace_repo
        txn = _make_txn(manager, repo, commit, branch, tmp_path)
        txn.begin()
        original = (repo / "src" / "example.py").read_bytes()
        (repo / "src" / "example.py").write_text("modified\n", encoding="utf-8")
        (repo / "src" / "extra.py").write_text("new\n", encoding="utf-8")
        txn.capture_and_restore()
        assert (repo / "src" / "example.py").read_bytes() == original
        assert not (repo / "src" / "extra.py").exists()
        assert manager.state(repo).dirty is False

    def test_metadata_seal_preserved_after_restore(
        self,
        workspace_repo: tuple[GitManager, Path, str, str],
        tmp_path: Path,
    ) -> None:
        manager, repo, commit, branch = workspace_repo
        seal = manager.capture_metadata_seal(repo, include_objects=True)
        txn = _make_txn(manager, repo, commit, branch, tmp_path)
        txn.begin()
        (repo / "src" / "example.py").write_text("changed\n", encoding="utf-8")
        txn.capture_and_restore()
        manager.assert_metadata_seal(repo, seal, include_objects=True)

    def test_untracked_new_file_removed_after_capture(
        self,
        workspace_repo: tuple[GitManager, Path, str, str],
        tmp_path: Path,
    ) -> None:
        manager, repo, commit, branch = workspace_repo
        txn = _make_txn(manager, repo, commit, branch, tmp_path)
        txn.begin()
        untracked = repo / "untracked.txt"
        untracked.write_text("new file", encoding="utf-8")
        txn.capture_and_restore()
        assert not untracked.exists()
        assert manager.state(repo).dirty is False

    def test_begin_rejects_dirty_workspace(
        self,
        workspace_repo: tuple[GitManager, Path, str, str],
        tmp_path: Path,
    ) -> None:
        manager, repo, commit, branch = workspace_repo
        (repo / "src" / "example.py").write_text("dirty\n", encoding="utf-8")
        txn = _make_txn(manager, repo, commit, branch, tmp_path)
        with pytest.raises(WorkspaceNotClean):
            txn.begin()


# ===================================================================
# 3. TestRunner -- environment, timeout, output redaction, pollution
# ===================================================================


class TestTestRunnerFaultInjection:
    """SafeTestRunner: environment isolation, process reaping, redaction."""

    @pytest.mark.asyncio
    async def test_environment_secret_not_leaked(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        tests = repo / "tests"
        tests.mkdir(parents=True)
        (tests / "test_env.py").write_text(
            "import os\n\n"
            "def test_no_secret():\n"
            "    assert 'AGENT_HUB_SECRET_TOKEN' not in os.environ\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AGENT_HUB_SECRET_TOKEN", "must-not-leak")
        runner = SafeTestRunner(CommandGuard(), timeout_seconds=30, max_output_bytes=4096)
        result = await runner.run(
            ["pytest", "-q", "tests/test_env.py"],
            repo=repo,
            runtime_directory=tmp_path / "runtime",
        )
        assert result.passed
        assert "must-not-leak" not in result.output

    @pytest.mark.asyncio
    async def test_timeout_kills_subprocess_tree(self, tmp_path: Path) -> None:
        """Runner must kill a hung subprocess tree and verify PID reaped."""
        pid_file = tmp_path / "child_pid.txt"
        repo = tmp_path / "repo"
        tests = repo / "tests"
        tests.mkdir(parents=True)

        grandchild = tmp_path / "grandchild.py"
        grandchild.write_text(
            f"import time, pathlib, os\n"
            f"pathlib.Path(r'{pid_file}').write_text(str(os.getpid()))\n"
            f"time.sleep(120)\n",
            encoding="utf-8",
        )

        (tests / "test_subprocess_tree.py").write_text(
            f"import subprocess, sys, time\n\n"
            f"def test_spawn_tree():\n"
            f"    p = subprocess.Popen(\n"
            f"        [sys.executable, r'{grandchild}'],\n"
            f"        close_fds=True,\n"
            f"    )\n"
            f"    time.sleep(120)\n",
            encoding="utf-8",
        )
        runner = SafeTestRunner(CommandGuard(), timeout_seconds=5, max_output_bytes=4096)
        result = await runner.run(
            ["pytest", "-q", "tests/test_subprocess_tree.py"],
            repo=repo,
            runtime_directory=tmp_path / "runtime",
        )
        assert result.timed_out
        assert not result.passed

        # PID file must exist (grandchild wrote it before sleep)
        assert pid_file.exists(), "Grandchild PID file not created"
        child_pid = int(pid_file.read_text().strip())

        # Bounded polling: wait up to 10s for process to be reaped
        deadline = _time.monotonic() + 10
        while _time.monotonic() < deadline:
            if not psutil.pid_exists(child_pid):
                break
            _time.sleep(0.5)

        assert not psutil.pid_exists(child_pid), (
            f"Child process {child_pid} still alive after timeout"
        )

    @pytest.mark.asyncio
    async def test_output_truncation(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        tests = repo / "tests"
        tests.mkdir(parents=True)
        (tests / "test_output.py").write_text(
            "def test_long():\n    print('x' * 5000)\n",
            encoding="utf-8",
        )
        runner = SafeTestRunner(CommandGuard(), timeout_seconds=30, max_output_bytes=64)
        result = await runner.run(
            ["pytest", "-q", "tests/test_output.py"],
            repo=repo,
            runtime_directory=tmp_path / "runtime",
        )
        assert result.output_truncated
        assert len(result.output.encode("utf-8")) <= 64

    @pytest.mark.asyncio
    async def test_runner_pollution_cleaned_by_transaction(
        self,
        workspace_repo: tuple[GitManager, Path, str, str],
        tmp_path: Path,
    ) -> None:
        """SafeTestRunner + WorkspaceTransaction: pollution detected and cleaned."""
        manager, repo, _commit, branch = workspace_repo

        # Commit test and gitignore baseline into the repo
        _git(repo, "config", "core.autocrlf", "false")
        gi = repo / ".gitignore"
        gi.write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        tests = repo / "tests"
        tests.mkdir()
        (tests / "test_pollution.py").write_text(
            "from pathlib import Path\n\n"
            "def test_creates_pollution():\n"
            "    Path('src/example.py').write_text('MUTATED\\n')\n"
            "    Path('untracked_junk.txt').write_text('junk')\n"
            "    pyc = Path('__pycache__')\n"
            "    pyc.mkdir(exist_ok=True)\n"
            "    (pyc / 'module.pyc').write_text('bytecode')\n"
            "    assert True\n",
            encoding="utf-8",
        )
        _git(repo, "add", ".")
        r = _git(repo, "commit", "-m", "add test and gitignore")
        assert r.returncode == 0, f"commit failed: {r.stderr}"

        # Capture pre-test baseline
        original_content = (repo / "src" / "example.py").read_bytes()
        seal = manager.capture_metadata_seal(repo, include_objects=True)
        new_commit = _git(repo, "rev-parse", "HEAD").stdout.strip()

        # Begin transaction and run the test
        txn = _make_txn(manager, repo, new_commit, branch, tmp_path)
        txn.begin()

        runner = SafeTestRunner(CommandGuard(), timeout_seconds=30, max_output_bytes=4096)
        result = await runner.run(
            ["pytest", "-q", "tests/test_pollution.py"],
            repo=repo,
            runtime_directory=tmp_path / "runner-runtime",
        )
        assert result.passed

        # capture_and_restore must clean all pollution
        txn.capture_and_restore()

        # Tracked content restored
        assert (repo / "src" / "example.py").read_bytes() == original_content

        # Untracked/ignored files and dirs removed
        assert not (repo / "untracked_junk.txt").exists()
        assert not (repo / "__pycache__").exists()

        # Repo clean
        assert manager.state(repo).dirty is False

        # Metadata seal preserved (pre-test seal, not regenerated)
        manager.assert_metadata_seal(repo, seal, include_objects=True)

    def test_redact_output_through_production_redactor(self) -> None:
        text = "Authorization: Bearer sk-proj-abcdef1234567890abcdef"
        redacted = _redact_output(text)
        assert "sk-proj-abcdef1234567890abcdef" not in redacted
        assert "[REDACTED]" in redacted

    def test_redact_output_aws_key(self) -> None:
        text = "Using AKIAIOSFODNN7EXAMPLE for upload"
        redacted = _redact_output(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in redacted


# ===================================================================
# 4. Lease -- expiration, takeover, stale fencing, run_fenced
# ===================================================================


class TestLeaseFaultInjection:
    """LockManager / MasterLeaseRepository must enforce fencing and expiry."""

    @pytest.mark.asyncio
    async def test_expired_master_takeover_fences_old_owner(
        self,
        database: Database,
    ) -> None:
        repo = MasterLeaseRepository(database)
        old = await repo.acquire(instance_id="master-old", process_id=101, ttl_seconds=5, now=BASE)
        new = await repo.acquire(
            instance_id="master-new",
            process_id=202,
            ttl_seconds=5,
            now=BASE + timedelta(seconds=6),
        )
        assert new.fencing_token > old.fencing_token
        with pytest.raises(LeaseLost):
            await repo.assert_valid(old, now=BASE + timedelta(seconds=7))

    @pytest.mark.asyncio
    async def test_concurrent_master_acquire_one_winner(
        self,
        database: Database,
    ) -> None:
        repo = MasterLeaseRepository(database)

        async def acquire(instance_id: str) -> object:
            try:
                return await repo.acquire(
                    instance_id=instance_id,
                    process_id=os.getpid(),
                    ttl_seconds=15,
                    now=BASE,
                )
            except LeaseUnavailable:
                return None

        results = await asyncio.gather(acquire("master-1"), acquire("master-2"))
        assert len([r for r in results if r is not None]) == 1

    @pytest.mark.asyncio
    async def test_stale_owner_run_fenced_blocked(
        self,
        database: Database,
        tmp_path: Path,
    ) -> None:
        """run_fenced() must reject stale lease and not execute the operation."""
        lease_repo = WorkspaceLeaseRepository(database)
        lock_manager = LockManager(lease_repo)

        old_lease = await lock_manager.acquire(
            session_id="sess-stale-fenced",
            owner_kind=WorkspaceOwnerKind.AGENT_TASK,
            owner_operation_id="task-old",
            owner_process_id=101,
            ttl_seconds=3,
            now=BASE,
        )

        # New owner takes over after TTL
        await lock_manager.acquire(
            session_id="sess-stale-fenced",
            owner_kind=WorkspaceOwnerKind.AGENT_TASK,
            owner_operation_id="task-new",
            owner_process_id=202,
            ttl_seconds=10,
            now=BASE + timedelta(seconds=4),
        )

        # Build HeldWorkspaceLease with stale lease
        held = HeldWorkspaceLease(lease=old_lease)
        marker = tmp_path / "marker.txt"
        operation_called = False

        def fenced_operation() -> str:
            nonlocal operation_called
            operation_called = True
            marker.write_text("executed")
            return "should-not-reach"

        with pytest.raises(LeaseLost):
            await lock_manager.run_fenced(
                held,
                session_id="sess-stale-fenced",
                ttl_seconds=5,
                operation=fenced_operation,
                now=BASE + timedelta(seconds=5),
            )

        assert not operation_called, "Operation must not be called on stale lease"
        assert not marker.exists(), "Marker file must not be created on stale lease"

    @pytest.mark.asyncio
    async def test_heartbeat_loss_detected(
        self,
        database: Database,
    ) -> None:
        repo = MasterLeaseRepository(database)
        lease = await repo.acquire(instance_id="master-hb", process_id=101, ttl_seconds=5, now=BASE)
        hb = await repo.heartbeat(lease, ttl_seconds=5, now=BASE + timedelta(seconds=3))
        assert hb.fencing_token == lease.fencing_token
        with pytest.raises(LeaseLost):
            await repo.assert_valid(lease, now=BASE + timedelta(seconds=10))


# ===================================================================
# 5. Artifact -- DB commit boundary, hash drift, quota
# ===================================================================


class TestArtifactFaultInjection:
    """ArtifactRepository: commit boundary, reconciliation, containment."""

    @pytest.mark.asyncio
    async def test_publish_detects_hash_drift_in_temp_file(
        self,
        artifact_store: ArtifactStore,
    ) -> None:
        content = b"legitimate content"
        staged = artifact_store.write_temp(
            artifact_id="art-drift",
            artifact_type="diff",
            content=content,
        )
        staged.tmp_path.write_bytes(b"TAMPERED")
        with pytest.raises(PathEscapeError, match=r"size|hash"):
            artifact_store.publish(staged)

    @pytest.mark.asyncio
    async def test_db_rollback_cleans_published_artifact(
        self,
        artifact_repo: ArtifactRepository,
        session_repo: SessionRepository,
        artifact_store: ArtifactStore,
    ) -> None:
        """INSERT failure -> transaction rolls back -> published file cleaned."""
        await _create_session(session_repo)
        content = b"will-rollback"

        original_tx_execute = Transaction.execute

        async def failing_tx_execute(self_tx, sql: str, params: tuple = ()):
            if "INSERT INTO artifacts" in sql:
                raise RuntimeError("simulated INSERT failure")
            return await original_tx_execute(self_tx, sql, params)

        with (
            mock_patch.object(Transaction, "execute", failing_tx_execute),
            pytest.raises(RuntimeError, match="INSERT failure"),
        ):
            await artifact_repo.create(
                artifact_id="art-rollback",
                session_id="sess-hub220",
                artifact_type=ArtifactType.DIFF,
                content=content,
            )

        final_path = artifact_store.resolve("art-rollback", "diff")
        assert not final_path.exists(), "Published file must be cleaned after rollback"
        assert await artifact_repo.list_by_session("sess-hub220") == []

    @pytest.mark.asyncio
    async def test_db_commit_success_post_error_reconciles(
        self,
        artifact_repo: ArtifactRepository,
        session_repo: SessionRepository,
        artifact_store: ArtifactStore,
        database: Database,
    ) -> None:
        """Post-commit error -> reconciliation returns committed ArtifactRecord."""
        await _create_session(session_repo)
        content = b"committed-then-error"

        commit_count = 0

        @asynccontextmanager
        async def commit_then_raise() -> AsyncIterator[Transaction]:
            nonlocal commit_count
            async with database.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    yield Transaction(conn)
                except BaseException:
                    await conn.rollback()
                    raise
                else:
                    await conn.commit()
                    commit_count += 1
                    raise RuntimeError("simulated post-commit error")

        with mock_patch.object(database, "immediate_transaction", commit_then_raise):
            record = await artifact_repo.create(
                artifact_id="art-post-commit",
                session_id="sess-hub220",
                artifact_type=ArtifactType.DIFF,
                content=content,
            )

        assert commit_count == 1, "Commit must have been reached"

        # Returned record is correct
        assert record.artifact_id == "art-post-commit"
        assert record.sha256 == hashlib.sha256(content).hexdigest()
        assert record.size_bytes == len(content)

        # The entire immutable metadata row must survive the post-commit error,
        # not merely the hash and size fields.
        persisted = await artifact_repo.get("art-post-commit")
        assert persisted == record

        # Final artifact file exists and matches
        final_path = artifact_store.resolve("art-post-commit", "diff")
        assert final_path.exists(), "Final artifact must survive post-commit error"
        assert final_path.read_bytes() == content

    @pytest.mark.asyncio
    async def test_create_rejects_missing_session(
        self,
        artifact_repo: ArtifactRepository,
    ) -> None:
        with pytest.raises(RecordNotFound):
            await artifact_repo.create(
                artifact_id="art-no-session",
                session_id="nonexistent-session",
                artifact_type=ArtifactType.DIFF,
                content=b"data",
            )

    @pytest.mark.asyncio
    async def test_quota_enforcement(
        self,
        database: Database,
        artifact_store: ArtifactStore,
        session_repo: SessionRepository,
    ) -> None:
        repo = ArtifactRepository(
            database,
            artifact_store,
            max_artifact_bytes=50,
            max_session_artifact_bytes=1024 * 1024,
        )
        await _create_session(session_repo)
        with pytest.raises(QuotaExceeded):
            await repo.create(
                artifact_id="art-too-big",
                session_id="sess-hub220",
                artifact_type=ArtifactType.DIFF,
                content=b"x" * 60,
            )

    @pytest.mark.asyncio
    async def test_path_escape_rejected(
        self,
        artifact_store: ArtifactStore,
    ) -> None:
        with pytest.raises((PathEscapeError, OSError)):
            artifact_store.write_temp(
                artifact_id="../../../etc/passwd",
                artifact_type="diff",
                content=b"evil",
            )

    @pytest.mark.asyncio
    async def test_publish_rejects_missing_temp(
        self,
        artifact_store: ArtifactStore,
    ) -> None:
        staged = artifact_store.write_temp(
            artifact_id="art-missing", artifact_type="diff", content=b"data"
        )
        staged.tmp_path.unlink(missing_ok=True)
        with pytest.raises(PathEscapeError, match="missing"):
            artifact_store.publish(staged)

    @pytest.mark.asyncio
    async def test_double_publish_rejected(
        self,
        artifact_store: ArtifactStore,
    ) -> None:
        staged = artifact_store.write_temp(
            artifact_id="art-double", artifact_type="diff", content=b"data"
        )
        artifact_store.publish(staged)
        with pytest.raises(PathEscapeError, match="state"):
            artifact_store.publish(staged)


# ===================================================================
# 6. SecureFile -- identity change detection
# ===================================================================


class TestSecureFileFaultInjection:
    """SecureWorkspaceRoot file operations must detect tampering."""

    def test_unlink_regular_rejects_directory(
        self,
        source_repo: Path,
    ) -> None:
        root = SecureWorkspaceRoot(source_repo)
        with pytest.raises((SecureFileError, OSError)):
            root.unlink_regular("src")
        root.close()

    def test_replace_rejects_after_identity_change(
        self,
        source_repo: Path,
        tmp_path: Path,
    ) -> None:
        root = SecureWorkspaceRoot(source_repo)
        backup = tmp_path / "backup"
        source_repo.rename(backup)
        attacker = Path(str(source_repo))
        attacker.mkdir()
        (attacker / "src").mkdir()
        (attacker / "src" / "example.py").write_bytes(b"evil")
        with pytest.raises((SecureFileError, OSError)):
            root.replace_regular("src/example.py", b"safe", mode=0o644)
        root.close()

    def test_root_close_is_idempotent(
        self,
        source_repo: Path,
    ) -> None:
        root = SecureWorkspaceRoot(source_repo)
        root.close()
        root.close()

    def test_root_captures_directory_metadata(
        self,
        source_repo: Path,
    ) -> None:
        root = SecureWorkspaceRoot(source_repo)
        assert root.path == source_repo.resolve()
        root.assert_root_identity()
        root.close()


# ===================================================================
# 7. GitManager -- index/object seal detects staging changes
# ===================================================================


class TestGitManagerIntegrity:
    """GitManager seals must detect staging and object changes."""

    def test_index_sha256_detects_staging(
        self,
        source_repo: Path,
        tmp_path: Path,
    ) -> None:
        manager = GitManager(tmp_path / "gm-profile")
        baseline = manager.index_sha256(source_repo)
        (source_repo / "src" / "new.py").write_text("new\n", encoding="utf-8")
        _git(source_repo, "add", "src/new.py")
        assert manager.index_sha256(source_repo) != baseline

    def test_metadata_seal_detects_object_change(
        self,
        source_repo: Path,
        tmp_path: Path,
    ) -> None:
        manager = GitManager(tmp_path / "gm-profile-2")
        seal = manager.capture_metadata_seal(source_repo, include_objects=True)
        (source_repo / "src" / "new.py").write_text("staged\n", encoding="utf-8")
        _git(source_repo, "add", "src/new.py")
        with pytest.raises(Exception, match="metadata"):
            manager.assert_metadata_seal(source_repo, seal, include_objects=True)

    def test_object_seal_detects_content_tampering(
        self,
        source_repo: Path,
        tmp_path: Path,
    ) -> None:
        manager = GitManager(tmp_path / "gm-profile-3")
        seal = manager.capture_metadata_seal(source_repo, include_objects=True)
        (source_repo / "src" / "example.py").write_text("TAMPERED\n", encoding="utf-8")
        _git(source_repo, "add", "src/example.py")
        with pytest.raises(Exception, match="metadata"):
            manager.assert_metadata_seal(source_repo, seal, include_objects=True)


# ===================================================================
# 8. .git/objects pollution detection
# ===================================================================


class TestGitObjectsPollution:
    """.git/objects must only contain legitimate entries."""

    def test_no_junk_in_git_objects(
        self,
        source_repo: Path,
    ) -> None:
        objects_dir = source_repo / ".git" / "objects"
        for item in objects_dir.iterdir():
            name = item.name
            if item.is_dir():
                is_hex = len(name) == 2 and all(c in "0123456789abcdef" for c in name)
                is_special = name in ("info", "pack")
                assert is_hex or is_special, f"Suspicious entry in .git/objects: {name}"
