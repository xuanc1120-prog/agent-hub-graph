from __future__ import annotations

import os
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest

from security.patch_guard import PatchGuard, PatchGuardDecision
from workspace.change_set import ChangeSetManifest, FileAction, FileChange
from workspace.git_manager import GitManager, GitManagerError
from workspace.secure_file import SecureWorkspaceRoot

_ZERO = "0" * 64
_ONE = "1" * 64
_COMMIT = "a" * 40


def _manifest(change: FileChange) -> ChangeSetManifest:
    return ChangeSetManifest(
        base_commit=_COMMIT,
        pre_state_hash=_ZERO,
        post_state_hash=_ONE,
        changes=(change,),
        staged_evidence_sha256=_ZERO,
        unstaged_evidence_sha256=_ZERO,
        status_evidence_sha256=_ZERO,
    )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo_with_file(tmp_path: Path) -> tuple[Path, GitManager, str]:
    repo = tmp_path / "git-repo"
    repo.mkdir()
    (repo / "README.md").write_text("before\n", encoding="utf-8")
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "add", "README.md")
    _git(
        repo,
        "-c",
        "user.name=Agent Hub Tests",
        "-c",
        "user.email=tests@agent-hub.local",
        "commit",
        "-m",
        "baseline",
    )
    return repo, GitManager(tmp_path / "git-profile"), _git(repo, "rev-parse", "HEAD")


def test_protected_existing_resource_requires_an_exact_consumed_grant(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    change = FileChange(
        action=FileAction.MODIFIED,
        path="pyproject.toml",
        before_sha256=_ZERO,
        after_sha256=_ONE,
        before_size=1,
        after_size=2,
    )
    patch = b"canonical patch"

    rejected = PatchGuard(repo).check(
        _manifest(change),
        patch,
        allowed_existing_files=["pyproject.toml"],
        allowed_new_files=[],
        expected_patch_sha256=sha256(patch).hexdigest(),
    )
    accepted = PatchGuard(repo).check(
        _manifest(change),
        patch,
        allowed_existing_files=["pyproject.toml"],
        allowed_new_files=[],
        granted_existing_files=["pyproject.toml"],
        expected_patch_sha256=sha256(patch).hexdigest(),
    )

    assert rejected.decision == PatchGuardDecision.REJECTED
    assert any(
        "privileged_resource_requires_consumed_grant" in reason for reason in rejected.reasons
    )
    assert accepted.decision == PatchGuardDecision.PASSED


def test_failed_merge_cleanup_removes_only_plain_created_entries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "created.txt").write_text("temporary\n", encoding="utf-8")
    nested = repo / "new" / "nested"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("temporary\n", encoding="utf-8")

    manager = GitManager(tmp_path / "git-profile")
    manager.remove_worktree_entries(
        repo,
        ["created.txt", "new/nested/file.txt", "new/nested", "new"],
    )

    assert not (repo / "created.txt").exists()
    assert not (repo / "new").exists()


def test_approved_nonempty_patch_is_staged_then_committed(tmp_path: Path) -> None:
    repo, manager, parent = _repo_with_file(tmp_path)
    (repo / "README.md").write_text("after\n", encoding="utf-8")
    patch = subprocess.run(
        ["git", "diff", "--binary", "--", "README.md"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    _git(repo, "restore", "--", "README.md")
    expected_index = manager.index_sha256(repo)
    manager.apply_check(repo, patch)
    manager.apply_patch(repo, patch)
    commit, _tree = manager.commit_approved_patch(
        repo,
        parent_commit=parent,
        paths=["README.md"],
        patch_bytes=patch,
        patch_sha256=sha256(patch).hexdigest(),
        post_state_hash=_ONE,
        session_id="session-test",
        workflow_run_id="run-test",
        change_set_id="changeset-test",
        approval_id="approval-test",
        expected_index_sha256=expected_index,
    )

    assert commit == _git(repo, "rev-parse", "HEAD")
    assert manager.state(repo).dirty is False
    assert (repo / "README.md").read_text(encoding="utf-8") == "after\n"


def test_approved_rename_uses_canonical_tree_not_name_only_paths(tmp_path: Path) -> None:
    repo, manager, parent = _repo_with_file(tmp_path)
    (repo / "README.md").rename(repo / "RENAMED.md")
    _git(repo, "add", "-A")
    patch = subprocess.run(
        ["git", "diff", "--cached", "--binary", "--find-renames"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    _git(repo, "reset", "--mixed", "HEAD")
    (repo / "RENAMED.md").rename(repo / "README.md")
    expected_index = manager.index_sha256(repo)
    manager.apply_check(repo, patch)
    manager.apply_patch(repo, patch)
    commit, _tree = manager.commit_approved_patch(
        repo,
        parent_commit=parent,
        paths=["README.md", "RENAMED.md"],
        patch_bytes=patch,
        patch_sha256=sha256(patch).hexdigest(),
        post_state_hash=_ONE,
        session_id="session-test",
        workflow_run_id="run-test",
        change_set_id="changeset-rename",
        approval_id="approval-test",
        expected_index_sha256=expected_index,
    )

    assert commit == _git(repo, "rev-parse", "HEAD")
    assert not (repo / "README.md").exists()
    assert (repo / "RENAMED.md").read_text(encoding="utf-8") == "before\n"


def test_failed_merge_cleanup_refuses_hardlinks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    original = tmp_path / "outside.txt"
    original.write_text("must remain\n", encoding="utf-8")
    try:
        os.link(original, repo / "created.txt")
    except OSError as error:
        pytest.skip(f"hardlinks unavailable: {error}")

    with pytest.raises(GitManagerError, match="secure rollback"):
        GitManager(tmp_path / "git-profile").remove_worktree_entries(repo, ["created.txt"])
    assert original.read_text(encoding="utf-8") == "must remain\n"


def test_failed_merge_cleanup_refuses_a_replaced_parent_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("must remain\n", encoding="utf-8")
    replaced_parent = repo / "created"
    replaced_parent.mkdir()
    temporary = replaced_parent / "file.txt"
    temporary.write_text("temporary\n", encoding="utf-8")
    probe = repo / "symlink-probe"
    try:
        probe.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")
    else:
        probe.unlink()

    original_unlink = SecureWorkspaceRoot.unlink_regular
    swapped = False

    def replace_parent_before_delete(
        secure_root: SecureWorkspaceRoot,
        relative: str,
        *,
        missing_ok: bool = True,
    ) -> bool:
        nonlocal swapped
        if not swapped:
            swapped = True
            temporary.unlink()
            replaced_parent.rmdir()
            replaced_parent.symlink_to(outside, target_is_directory=True)
        return original_unlink(secure_root, relative, missing_ok=missing_ok)

    monkeypatch.setattr(SecureWorkspaceRoot, "unlink_regular", replace_parent_before_delete)

    with pytest.raises(GitManagerError, match="secure rollback"):
        GitManager(tmp_path / "git-profile").remove_worktree_entries(
            repo,
            ["created/file.txt"],
        )

    assert sentinel.read_text(encoding="utf-8") == "must remain\n"
    assert replaced_parent.is_symlink()
