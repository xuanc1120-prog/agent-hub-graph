from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from workspace.change_set import FileAction
from workspace.git_manager import GitManager
from workspace.transaction import (
    WorkspaceNotClean,
    WorkspaceRestoreError,
    WorkspaceTransaction,
    WorkspaceTransactionError,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _session_repo(
    source_repo: Path,
    tmp_path: Path,
) -> tuple[GitManager, Path, str, str]:
    manager = GitManager(tmp_path / "git-profile")
    source = manager.inspect_source_repository(source_repo)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    repo = workspace_root / "session-one" / "repo"
    state = manager.create_session_repository(
        source=source,
        destination=repo,
        session_id="session-one",
    )
    return manager, repo, state.commit, state.branch


def _transaction(
    manager: GitManager,
    repo: Path,
    commit: str,
    branch: str,
    tmp_path: Path,
    **limits: int,
) -> WorkspaceTransaction:
    return WorkspaceTransaction(
        manager,
        repo,
        base_commit=commit,
        expected_branch=branch,
        temp_directory=tmp_path / "transaction-temp",
        **limits,
    )


def test_capture_canonical_patch_and_restore_mixed_changes(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    object_seal = manager.capture_metadata_seal(
        repo,
        include_objects=True,
    )
    original = (repo / "src" / "example.py").read_bytes()
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()

    (repo / "src" / "example.py").write_text("value = 2\n", encoding="utf-8")
    (repo / "src" / "new.py").write_text("created = True\n", encoding="utf-8")
    result = transaction.capture_and_restore()

    actions = {(change.action, change.path) for change in result.manifest.changes}
    assert (FileAction.MODIFIED, "src/example.py") in actions
    assert (FileAction.CREATED, "src/new.py") in actions
    assert result.patch_bytes.startswith(b"diff --git ")
    assert result.status_evidence
    assert len(result.preimages) == 1
    assert result.preimages[0].content == original
    assert (repo / "src" / "example.py").read_bytes() == original
    assert not (repo / "src" / "new.py").exists()
    assert manager.state(repo).dirty is False
    manager.assert_metadata_seal(
        repo,
        object_seal,
        include_objects=True,
    )


def test_staged_and_unstaged_edits_produce_one_final_patch_without_index_pollution(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    baseline_index = manager.index_sha256(repo)
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()

    target = repo / "src" / "example.py"
    target.write_text("staged = True\n", encoding="utf-8")
    _git(repo, "add", "src/example.py")
    target.write_text("final = True\n", encoding="utf-8")
    result = transaction.capture_and_restore()

    assert len(result.manifest.changes) == 1
    assert result.manifest.changes[0].action == FileAction.MODIFIED
    assert b"final = True" in result.patch_bytes
    assert b"staged = True" not in result.patch_bytes
    assert result.staged_evidence
    assert result.unstaged_evidence
    assert manager.index_sha256(repo) == baseline_index
    assert manager.state(repo).dirty is False


def test_capture_delete_and_rename_then_restore(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()

    os.replace(repo / "src" / "example.py", repo / "src" / "renamed.py")
    (repo / "README.md").unlink()
    result = transaction.capture_and_restore()

    actions = {change.action for change in result.manifest.changes}
    assert FileAction.RENAMED in actions
    assert FileAction.DELETED in actions
    assert (repo / "src" / "example.py").exists()
    assert not (repo / "src" / "renamed.py").exists()
    assert (repo / "README.md").exists()
    assert manager.state(repo).dirty is False


def test_ignored_preimages_are_restored_and_new_ignored_files_removed(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    (fixture_source_repo / ".gitignore").write_text("cache/\n", encoding="utf-8")
    _git(fixture_source_repo, "add", ".gitignore")
    _git(
        fixture_source_repo,
        "-c",
        "user.name=Agent Hub Tests",
        "-c",
        "user.email=tests@agent-hub.local",
        "commit",
        "-m",
        "ignore cache",
    )
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    cache = repo / "cache"
    cache.mkdir()
    existing = cache / "state.txt"
    existing.write_text("before\n", encoding="utf-8", newline="\n")
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()

    existing.write_text("after\n", encoding="utf-8", newline="\n")
    (cache / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")
    result = transaction.capture_and_restore()

    assert result.manifest.ignored_files_touched == (
        "cache/new.txt",
        "cache/state.txt",
    )
    actions = {(change.action, change.path) for change in result.manifest.changes}
    assert (FileAction.CREATED, "cache/new.txt") in actions
    assert (FileAction.MODIFIED, "cache/state.txt") in actions
    ignored_preimage = next(
        preimage for preimage in result.preimages if preimage.path == "cache/state.txt"
    )
    assert ignored_preimage.baseline_ignored is True
    assert ignored_preimage.content == b"before\n"
    assert existing.read_text(encoding="utf-8") == "before\n"
    assert not (cache / "new.txt").exists()
    assert manager.state(repo).dirty is False


def test_deleted_ignored_file_and_parent_are_replayable_and_restored(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    (fixture_source_repo / ".gitignore").write_text("cache/\n", encoding="utf-8")
    _git(fixture_source_repo, "add", ".gitignore")
    _git(
        fixture_source_repo,
        "-c",
        "user.name=Agent Hub Tests",
        "-c",
        "user.email=tests@agent-hub.local",
        "commit",
        "-m",
        "ignore cache",
    )
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    cache = repo / "cache"
    cache.mkdir()
    existing = cache / "state.txt"
    existing.write_text("before\n", encoding="utf-8", newline="\n")
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()

    existing.unlink()
    cache.rmdir()
    result = transaction.capture_and_restore()

    deleted = next(change for change in result.manifest.changes if change.path == "cache/state.txt")
    assert deleted.action == FileAction.DELETED
    assert result.manifest.ignored_files_touched == ("cache/state.txt",)
    preimage = next(preimage for preimage in result.preimages if preimage.path == "cache/state.txt")
    assert preimage.baseline_ignored is True
    assert preimage.content == b"before\n"
    assert existing.read_text(encoding="utf-8") == "before\n"
    assert manager.state(repo).dirty is False


def test_resource_limit_failure_still_restores_task_paths(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    transaction = _transaction(
        manager,
        repo,
        commit,
        branch,
        tmp_path,
        max_created_bytes=8,
    )
    transaction.begin()
    created = repo / "generated" / "nested" / "large.py"
    created.parent.mkdir(parents=True)
    created.write_text("x" * 100, encoding="utf-8")

    with pytest.raises(WorkspaceTransactionError, match="task-created bytes"):
        transaction.capture_and_restore()

    assert not created.exists()
    assert not (repo / "generated").exists()
    assert manager.state(repo).dirty is False


def test_begin_rejects_dirty_or_wrong_base(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    (repo / "src" / "example.py").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(WorkspaceNotClean, match="clean"):
        _transaction(manager, repo, commit, branch, tmp_path).begin()
    with pytest.raises(WorkspaceNotClean, match="HEAD"):
        _transaction(manager, repo, "b" * 40, branch, tmp_path).begin()


def test_head_change_is_detected_without_global_reset(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()
    _git(
        repo,
        "-c",
        "user.name=Agent Hub Tests",
        "-c",
        "user.email=tests@agent-hub.local",
        "commit",
        "--allow-empty",
        "-m",
        "unauthorized commit",
    )

    with pytest.raises(WorkspaceRestoreError, match="control metadata"):
        transaction.capture_and_restore()

    assert _git(repo, "rev-parse", "HEAD") != commit


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows symlink creation requires host-specific privileges",
)
def test_created_symlink_is_never_followed_or_deleted_implicitly(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    (repo / "src" / "link.py").symlink_to(outside)

    with pytest.raises(WorkspaceRestoreError):
        transaction.capture_and_restore()

    assert outside.read_text(encoding="utf-8") == "keep\n"


def test_git_control_metadata_tampering_orphans_before_capture(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()
    config = repo / ".git" / "config"
    config.write_text(
        f'{config.read_text(encoding="utf-8")}\n[filter "unsafe"]\nprocess = unsafe\n',
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceRestoreError, match="control metadata"):
        transaction.capture_and_restore()


def test_inventory_limit_restores_ignored_preimage_and_created_directories(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    (fixture_source_repo / ".gitignore").write_text("cache/\n", encoding="utf-8")
    _git(fixture_source_repo, "add", ".gitignore")
    _git(
        fixture_source_repo,
        "-c",
        "user.name=Agent Hub Tests",
        "-c",
        "user.email=tests@agent-hub.local",
        "commit",
        "-m",
        "ignore cache",
    )
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    cache = repo / "cache"
    cache.mkdir()
    ignored = cache / "state.txt"
    ignored.write_text("before\n", encoding="utf-8")
    transaction = _transaction(
        manager,
        repo,
        commit,
        branch,
        tmp_path,
        max_inventory_bytes=1024 * 1024,
    )
    transaction.begin()
    ignored.write_text("after\n", encoding="utf-8")
    created = repo / "generated" / "nested" / "large.bin"
    created.parent.mkdir(parents=True)
    created.write_bytes(b"x" * (2 * 1024 * 1024))

    with pytest.raises(WorkspaceTransactionError, match="inventory byte limit"):
        transaction.capture_and_restore()

    assert ignored.read_text(encoding="utf-8") == "before\n"
    assert not (repo / "generated").exists()
    assert manager.state(repo).dirty is False


def test_forbidden_control_patch_is_replayed_only_in_disposable_clone(
    fixture_source_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()
    control_file = repo / ".gitattributes"
    control_file.write_text("*.py filter=unsafe\n", encoding="utf-8")

    replay_repositories: list[Path] = []
    original_apply_check = manager.apply_check

    def observe_replay(replay_repo: Path, patch_bytes: bytes) -> None:
        assert replay_repo.resolve() != repo.resolve()
        assert not control_file.exists()
        replay_repositories.append(replay_repo)
        original_apply_check(replay_repo, patch_bytes)

    monkeypatch.setattr(manager, "apply_check", observe_replay)
    result = transaction.capture_and_restore()

    assert result.manifest.changes[0].path == ".gitattributes"
    assert len(replay_repositories) == 1
    assert not replay_repositories[0].exists()
    assert not control_file.exists()
