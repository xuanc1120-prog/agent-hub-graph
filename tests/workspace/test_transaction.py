from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from protocol import PrivilegeAction
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


def test_capability_seal_must_match_workspace_transaction_preimage(
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
        "--message",
        "add ignored cache",
    )
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    target = repo / "cache" / "settings.json"
    target.parent.mkdir()
    original = b'{"name":"demo"}'
    changed = b'{"name":"prod"}'
    assert len(original) == len(changed)
    target.write_bytes(original)
    from workflow.capability_broker import inspect_capability_resource

    seal = inspect_capability_resource(
        repo,
        PrivilegeAction.EDIT_PROJECT_CONFIG,
        "cache/settings.json",
    ).as_dict()
    assert seal["mode"] == stat.S_IMODE(target.stat().st_mode)

    transaction = WorkspaceTransaction(
        manager,
        repo,
        base_commit=commit,
        expected_branch=branch,
        temp_directory=tmp_path / "transaction-temp-positive",
        sealed_preimages={"cache/settings.json": seal},
    )
    transaction.begin()
    transaction.close()

    legacy_seal = dict(seal)
    legacy_seal["mode"] = target.stat().st_mode
    legacy_transaction = WorkspaceTransaction(
        manager,
        repo,
        base_commit=commit,
        expected_branch=branch,
        temp_directory=tmp_path / "transaction-temp-legacy-mode",
        sealed_preimages={"cache/settings.json": legacy_seal},
    )
    legacy_transaction.begin()
    legacy_transaction.close()

    target.write_bytes(changed)

    transaction = WorkspaceTransaction(
        manager,
        repo,
        base_commit=commit,
        expected_branch=branch,
        temp_directory=tmp_path / "transaction-temp",
        sealed_preimages={"cache/settings.json": seal},
    )
    with pytest.raises(WorkspaceNotClean, match="sealed capability resource"):
        transaction.begin()


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


def test_staged_rename_restores_source_target_and_index(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    baseline_index = manager.index_sha256(repo)
    original = (repo / "src" / "example.py").read_bytes()
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()

    _git(repo, "mv", "src/example.py", "src/renamed.py")
    result = transaction.capture_and_restore()

    assert any(
        change.action == FileAction.RENAMED
        and change.old_path == "src/example.py"
        and change.path == "src/renamed.py"
        for change in result.manifest.changes
    )
    assert (repo / "src" / "example.py").read_bytes() == original
    assert not (repo / "src" / "renamed.py").exists()
    assert manager.index_sha256(repo) == baseline_index
    assert manager.state(repo).dirty is False


def test_staged_rename_then_edit_restores_both_inventory_paths(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    baseline_index = manager.index_sha256(repo)
    original = (repo / "src" / "example.py").read_bytes()
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()

    _git(repo, "mv", "src/example.py", "src/renamed.py")
    (repo / "src" / "renamed.py").write_text(
        "renamed_and_modified = True\n",
        encoding="utf-8",
    )
    result = transaction.capture_and_restore()

    assert b"renamed_and_modified = True" in result.patch_bytes
    assert (repo / "src" / "example.py").read_bytes() == original
    assert not (repo / "src" / "renamed.py").exists()
    assert manager.index_sha256(repo) == baseline_index
    assert manager.state(repo).dirty is False


def test_non_git_mode_change_fails_closed_after_exact_restore(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    target = repo / "src" / "example.py"
    baseline_mode = target.stat().st_mode & 0o777
    changed_mode = baseline_mode & ~0o222
    if changed_mode == baseline_mode:
        changed_mode = baseline_mode | 0o200
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()

    target.chmod(changed_mode)
    assert target.stat().st_mode & 0o777 == changed_mode
    with pytest.raises(WorkspaceTransactionError):
        transaction.capture_and_restore()

    assert target.stat().st_mode & 0o777 == baseline_mode
    assert manager.state(repo).dirty is False


def test_lstat_to_open_hardlink_swap_never_reads_or_overwrites_external_file(
    fixture_source_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    target = repo / "src" / "example.py"
    original = target.read_bytes()
    external = tmp_path / "outside-secret.txt"
    external_secret = b"AWS_SECRET_ACCESS_KEY=OUTSIDE_SECRET\n"
    external.write_bytes(external_secret)
    displaced = tmp_path / "displaced-task-file.txt"
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()
    original_assert = transaction._assert_plain_entry
    target.write_text("task_change = True\n", encoding="utf-8")
    swapped = False

    def swap_after_lstat(path: Path, *, expect_directory: bool) -> None:
        nonlocal swapped
        original_assert(path, expect_directory=expect_directory)
        if path == target and not expect_directory and not swapped:
            swapped = True
            os.replace(target, displaced)
            os.link(external, target)

    monkeypatch.setattr(transaction, "_assert_plain_entry", swap_after_lstat)

    with pytest.raises(WorkspaceTransactionError):
        transaction.capture_and_restore()

    assert swapped is True
    assert external.read_bytes() == external_secret
    assert target.read_bytes() == original
    assert manager.state(repo).dirty is False


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows symlink creation requires host-specific privileges",
)
def test_lstat_to_open_symlink_swap_never_reads_external_file(
    fixture_source_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    target = repo / "src" / "example.py"
    original = target.read_bytes()
    external = tmp_path / "outside-secret.txt"
    external_secret = b"AWS_SECRET_ACCESS_KEY=OUTSIDE_SECRET\n"
    external.write_bytes(external_secret)
    displaced = tmp_path / "displaced-task-file.txt"
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()
    original_assert = transaction._assert_plain_entry
    target.write_text("task_change = True\n", encoding="utf-8")
    swapped = False

    def swap_after_lstat(path: Path, *, expect_directory: bool) -> None:
        nonlocal swapped
        original_assert(path, expect_directory=expect_directory)
        if path == target and not expect_directory and not swapped:
            swapped = True
            os.replace(target, displaced)
            target.symlink_to(external)

    monkeypatch.setattr(transaction, "_assert_plain_entry", swap_after_lstat)

    with pytest.raises(WorkspaceTransactionError):
        transaction.capture_and_restore()

    assert swapped is True
    assert external.read_bytes() == external_secret
    assert target.read_bytes() == original
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


def test_ignored_non_git_mode_change_fails_closed_after_exact_restore(
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
    target = cache / "state.txt"
    target.write_text("baseline\n", encoding="utf-8")
    baseline_mode = target.stat().st_mode & 0o777
    changed_mode = baseline_mode & ~0o222
    if changed_mode == baseline_mode:
        changed_mode = baseline_mode | 0o200
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()

    target.chmod(changed_mode)
    assert target.stat().st_mode & 0o777 == changed_mode
    with pytest.raises(WorkspaceTransactionError) as captured:
        transaction.capture_and_restore()

    assert target.read_text(encoding="utf-8") == "baseline\n"
    assert target.stat().st_mode & 0o777 == baseline_mode, (
        f"capture={captured.value!r}; cause={captured.value.__cause__!r}"
    )
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


def test_untouched_ignored_baseline_is_not_exported_as_a_preimage(
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
    untouched = cache / "state.txt"
    untouched.write_text("baseline-only\n", encoding="utf-8")
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()

    created = repo / "docs" / "new.md"
    created.parent.mkdir()
    created.write_text("new\n", encoding="utf-8")
    result = transaction.capture_and_restore()

    assert result.manifest.ignored_files_touched == ()
    assert all(preimage.path != "cache/state.txt" for preimage in result.preimages)
    assert untouched.read_text(encoding="utf-8") == "baseline-only\n"
    assert not created.exists()
    assert manager.state(repo).dirty is False


@pytest.mark.parametrize(
    "sensitive_path",
    [
        ".env",
        ".env.local",
        ".envrc",
        ".envrc.local",
        "credentials.json",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".yarnrc.yml",
        ".aws/credentials",
        ".azure/accessTokens.json",
        ".cargo/credentials.toml",
        ".config/gh/hosts.yml",
        ".docker/config.json",
        ".gradle/gradle.properties",
        ".kube/config",
        ".m2/settings.xml",
        ".terraform.d/credentials.tfrc.json",
        "secrets/deploy.key",
        "gradle.properties",
        "NuGet.Config",
        "pip.conf",
    ],
)
def test_begin_rejects_sensitive_ignored_baseline_before_agent_execution(
    fixture_source_repo: Path,
    tmp_path: Path,
    sensitive_path: str,
) -> None:
    (fixture_source_repo / ".gitignore").write_text(
        f"{sensitive_path}\n",
        encoding="utf-8",
    )
    _git(fixture_source_repo, "add", ".gitignore")
    _git(
        fixture_source_repo,
        "-c",
        "user.name=Agent Hub Tests",
        "-c",
        "user.email=tests@agent-hub.local",
        "commit",
        "-m",
        "ignore sensitive file",
    )
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    sensitive = repo / sensitive_path
    sensitive.parent.mkdir(parents=True, exist_ok=True)
    sensitive.write_text("must-not-be-exported\n", encoding="utf-8")

    with pytest.raises(WorkspaceNotClean, match="forbidden or unsafe"):
        _transaction(manager, repo, commit, branch, tmp_path).begin()

    assert sensitive.read_text(encoding="utf-8") == "must-not-be-exported\n"


@pytest.mark.parametrize(
    "credential_path,content",
    [
        (
            ".cargo/credentials.toml",
            '[registry]\ntoken = "cioabcdefghijklmnopqrstuvwxyz0123456789"\n',
        ),
        (
            ".m2/settings.xml",
            "<settings><password>maven-secret</password></settings>\n",
        ),
        (
            ".config/gh/hosts.yml",
            "github.com:\n  oauth_token: github-cli-secret\n",
        ),
        (
            ".terraform.d/credentials.tfrc.json",
            '{"credentials":{"app.terraform.io":{"token":"terraform-secret"}}}\n',
        ),
        (
            ".gradle/gradle.properties",
            "repositoryPassword=gradle-secret\n",
        ),
    ],
)
def test_new_ignored_credential_store_is_rejected_and_removed(
    fixture_source_repo: Path,
    tmp_path: Path,
    credential_path: str,
    content: str,
) -> None:
    (fixture_source_repo / ".gitignore").write_text(
        f"{credential_path}\n",
        encoding="utf-8",
    )
    _git(fixture_source_repo, "add", ".gitignore")
    _git(
        fixture_source_repo,
        "-c",
        "user.name=Agent Hub Tests",
        "-c",
        "user.email=tests@agent-hub.local",
        "commit",
        "-m",
        "ignore credential store",
    )
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()
    credential = repo / credential_path
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text(content, encoding="utf-8")

    with pytest.raises(WorkspaceTransactionError, match="forbidden path"):
        transaction.capture_and_restore()

    assert not credential.exists()
    assert manager.state(repo).dirty is False


def test_begin_rejects_credential_content_in_generic_ignored_file(
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
    ignored = cache / "local.cfg"
    ignored.write_text(
        "api_key=credential-value-that-must-not-persist\n",
        encoding="utf-8",
    )

    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    with pytest.raises(WorkspaceNotClean, match="cannot enter artifacts"):
        transaction.begin()


def test_changed_ignored_credential_content_is_rejected_and_restored(
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
    ignored = cache / "local.cfg"
    ignored.write_text("mode=demo\n", encoding="utf-8")
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()

    ignored.write_text(
        "sqlalchemy.url = oracle+cx_oracle://agent:secret@db/app\n",
        encoding="utf-8",
    )
    with pytest.raises(
        WorkspaceTransactionError,
        match="credential-like content",
    ):
        transaction.capture_and_restore()

    assert ignored.read_text(encoding="utf-8") == "mode=demo\n"
    assert manager.state(repo).dirty is False


def test_ignored_file_drift_after_secret_scan_fails_closed(
    fixture_source_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    ignored = cache / "state.toml"
    ignored.write_text('mode = "before"\n', encoding="utf-8")
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()
    ignored.write_text('mode = "safe-after"\n', encoding="utf-8")

    original_validate = transaction._validate_ignored_capture

    def replace_after_validation(*args: object, **kwargs: object) -> None:
        original_validate(*args, **kwargs)
        ignored.write_text(
            'token = "cioabcdefghijklmnopqrstuvwxyz0123456789"\n',
            encoding="utf-8",
        )

    monkeypatch.setattr(
        transaction,
        "_validate_ignored_capture",
        replace_after_validation,
    )

    with pytest.raises(
        WorkspaceTransactionError,
        match="changed after inventory",
    ):
        transaction.capture_and_restore()

    assert ignored.read_text(encoding="utf-8") == 'mode = "before"\n'
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


def test_transaction_close_releases_root_after_begin(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    manager, repo, commit, branch = _session_repo(fixture_source_repo, tmp_path)
    transaction = _transaction(manager, repo, commit, branch, tmp_path)
    transaction.begin()

    transaction.close()
    transaction.abort()

    assert transaction._secure_root is None
    with pytest.raises(WorkspaceTransactionError, match="not active"):
        transaction.capture_and_restore()


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
