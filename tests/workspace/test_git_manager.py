from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from workspace.git_manager import GitManager, GitManagerError


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_creates_independent_remote_free_session_clone(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    manager = GitManager(tmp_path / "git-profile")
    source = manager.inspect_source_repository(fixture_source_repo)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    destination = workspace_root / "session-one" / "repo"

    state = manager.create_session_repository(
        source=source,
        destination=destination,
        session_id="session-one",
    )

    assert state.commit == source.commit
    assert state.branch == "agent-hub/session/session-one"
    assert state.dirty is False
    assert _git(destination, "remote") == ""
    assert _git(fixture_source_repo, "status", "--porcelain") == ""
    config = (destination / ".git" / "config").read_text(encoding="utf-8")
    assert str(fixture_source_repo) not in config
    assert str(fixture_source_repo).replace("\\", "/") not in config


def test_session_clone_does_not_follow_later_source_changes(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    manager = GitManager(tmp_path / "git-profile")
    source = manager.inspect_source_repository(fixture_source_repo)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    destination = workspace_root / "session-one" / "repo"
    manager.create_session_repository(
        source=source,
        destination=destination,
        session_id="session-one",
    )

    (fixture_source_repo / "README.md").write_text("source changed\n", encoding="utf-8")

    assert (destination / "README.md").read_text(encoding="utf-8") != "source changed\n"
    assert manager.state(destination).dirty is False


def test_source_dirty_state_is_reported(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    (fixture_source_repo / "src" / "example.py").write_text("dirty = True\n", encoding="utf-8")

    source = GitManager(tmp_path / "git-profile").inspect_source_repository(fixture_source_repo)

    assert source.dirty is True


def test_rejects_submodule_metadata(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    (fixture_source_repo / ".gitmodules").write_text(
        '[submodule "unsafe"]\n\tpath = unsafe\n\turl = ../unsafe\n',
        encoding="utf-8",
    )
    _git(fixture_source_repo, "add", ".gitmodules")
    _git(
        fixture_source_repo,
        "-c",
        "user.name=Agent Hub Tests",
        "-c",
        "user.email=tests@agent-hub.local",
        "commit",
        "-m",
        "add submodule metadata",
    )

    with pytest.raises(GitManagerError, match=r"\.gitmodules"):
        GitManager(tmp_path / "git-profile").inspect_source_repository(fixture_source_repo)


def test_rejects_external_content_filter(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    (fixture_source_repo / ".gitattributes").write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n",
        encoding="utf-8",
    )
    _git(fixture_source_repo, "add", ".gitattributes")
    _git(
        fixture_source_repo,
        "-c",
        "user.name=Agent Hub Tests",
        "-c",
        "user.email=tests@agent-hub.local",
        "commit",
        "-m",
        "add filter",
    )

    with pytest.raises(GitManagerError, match="content filters"):
        GitManager(tmp_path / "git-profile").inspect_source_repository(fixture_source_repo)


def test_rejects_option_like_base_ref(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    with pytest.raises(GitManagerError, match="base_ref"):
        GitManager(tmp_path / "git-profile").inspect_source_repository(
            fixture_source_repo,
            base_ref="--upload-pack=bad",
        )


def test_cleanup_is_contained_to_workspace_root(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    manager = GitManager(tmp_path / "git-profile")
    source = manager.inspect_source_repository(fixture_source_repo)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    destination = workspace_root / "session-one" / "repo"
    manager.create_session_repository(
        source=source,
        destination=destination,
        session_id="session-one",
    )

    manager.remove_session_repository(destination, allowed_root=workspace_root)

    assert not destination.parent.exists()
    with pytest.raises(GitManagerError, match="outside"):
        manager.remove_session_repository(
            fixture_source_repo / "repo",
            allowed_root=workspace_root,
        )


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows symlink creation requires host-specific privileges",
)
def test_cleanup_does_not_follow_workspace_symlink(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    manager = GitManager(tmp_path / "git-profile")
    source = manager.inspect_source_repository(fixture_source_repo)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    destination = workspace_root / "session-one" / "repo"
    manager.create_session_repository(
        source=source,
        destination=destination,
        session_id="session-one",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("keep\n", encoding="utf-8")
    (destination / "escape").symlink_to(outside, target_is_directory=True)

    manager.remove_session_repository(destination, allowed_root=workspace_root)

    assert marker.read_text(encoding="utf-8") == "keep\n"
    assert not destination.parent.exists()


@pytest.mark.parametrize("relative_path", ["config", "info/attributes"])
def test_git_metadata_seal_rejects_control_file_changes(
    fixture_source_repo: Path,
    tmp_path: Path,
    relative_path: str,
) -> None:
    manager = GitManager(tmp_path / "git-profile")
    source = manager.inspect_source_repository(fixture_source_repo)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    destination = workspace_root / "session-one" / "repo"
    manager.create_session_repository(
        source=source,
        destination=destination,
        session_id="session-one",
    )
    seal = manager.capture_metadata_seal(destination)
    target = destination / ".git" / Path(relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    original = target.read_text(encoding="utf-8") if target.exists() else ""
    target.write_text(f'{original}\n[filter "unsafe"]\nprocess = unsafe\n', encoding="utf-8")

    with pytest.raises(GitManagerError, match="control metadata changed"):
        manager.assert_metadata_seal(destination, seal)


def test_git_metadata_seal_allows_staged_index_and_object_updates(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    manager = GitManager(tmp_path / "git-profile")
    source = manager.inspect_source_repository(fixture_source_repo)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    destination = workspace_root / "session-one" / "repo"
    manager.create_session_repository(
        source=source,
        destination=destination,
        session_id="session-one",
    )
    seal = manager.capture_metadata_seal(destination)
    (destination / "src" / "example.py").write_text("staged = True\n", encoding="utf-8")
    _git(destination, "add", "src/example.py")

    manager.assert_metadata_seal(destination, seal)


def test_git_metadata_seal_rejects_object_updates_when_requested(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    manager = GitManager(tmp_path / "git-profile")
    source = manager.inspect_source_repository(fixture_source_repo)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    destination = workspace_root / "session-one" / "repo"
    manager.create_session_repository(
        source=source,
        destination=destination,
        session_id="session-one",
    )
    seal = manager.capture_metadata_seal(
        destination,
        include_objects=True,
    )
    (destination / ".git" / "objects" / "agent-hub-pollution").write_bytes(b"pollution")

    with pytest.raises(GitManagerError, match="control metadata changed"):
        manager.assert_metadata_seal(
            destination,
            seal,
            include_objects=True,
        )


def test_private_temporary_directory_is_owner_only_and_safely_removed(
    tmp_path: Path,
) -> None:
    manager = GitManager(tmp_path / "git-profile")
    private_root = manager.create_private_temporary_directory(prefix="ah-test-private-")

    assert private_root.parent == Path(tempfile.gettempdir()).resolve(strict=True)
    assert private_root.name.startswith("ah-test-private-")
    if os.name != "nt":
        assert stat.S_IMODE(private_root.stat().st_mode) == 0o700

    nested = private_root / "workspace"
    nested.mkdir()
    (nested / "source.txt").write_text("private\n", encoding="utf-8")
    manager.remove_private_temporary_directory(private_root)

    assert not private_root.exists()


def test_private_temporary_cleanup_rejects_non_generated_path(
    tmp_path: Path,
) -> None:
    manager = GitManager(tmp_path / "git-profile")
    unrelated = tmp_path / "ah-unrelated"
    unrelated.mkdir()

    with pytest.raises(GitManagerError, match="not a generated root"):
        manager.remove_private_temporary_directory(unrelated)

    assert unrelated.exists()
