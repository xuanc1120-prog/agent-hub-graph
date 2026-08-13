from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

import workspace.secure_file as secure_file
from workspace.secure_file import (
    SecureFileError,
    SecureWorkspaceRoot,
    open_verified_binary,
    set_verified_mode,
)


def test_verified_binary_reads_plain_single_link_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "safe.txt"
    target.write_bytes(b"safe content")

    with open_verified_binary(repo, "safe.txt") as stream:
        assert stream.read() == b"safe content"


def test_verified_binary_rejects_hardlink(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside secret")
    os.link(outside, repo / "captured.txt")

    with (
        pytest.raises(SecureFileError, match="single-link"),
        open_verified_binary(repo, "captured.txt"),
    ):
        pass


def test_verified_binary_rejects_final_symlink(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside secret")
    target = repo / "captured.txt"
    try:
        target.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    with pytest.raises(SecureFileError), open_verified_binary(repo, "captured.txt"):
        pass


def test_verified_binary_rejects_symlinked_parent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"outside secret")
    link = repo / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink creation unavailable: {error}")

    with pytest.raises(SecureFileError), open_verified_binary(repo, "linked/secret.txt"):
        pass


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_verified_binary_rejects_windows_junction_parent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"outside secret")
    link = repo / "linked"
    command = os.environ.get("COMSPEC")
    if not command:
        pytest.skip("COMSPEC is unavailable")
    result = subprocess.run(
        [
            command,
            "/d",
            "/c",
            "mklink",
            "/J",
            str(link),
            str(outside),
        ],
        capture_output=True,
        check=False,
        shell=False,
    )
    if result.returncode != 0:
        pytest.skip("junction creation is unavailable")

    with pytest.raises(SecureFileError), open_verified_binary(repo, "linked/secret.txt"):
        pass


def test_set_verified_mode_restores_mode_through_handle(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "mode.txt"
    target.write_bytes(b"mode")
    original = stat.S_IMODE(target.stat().st_mode)
    changed = 0o444 if os.name == "nt" else 0o600
    if changed == original:
        changed = 0o400
    os.chmod(target, changed)
    assert stat.S_IMODE(target.stat().st_mode) != original

    set_verified_mode(repo, "mode.txt", original)

    assert stat.S_IMODE(target.stat().st_mode) == original


def _create_directory_link(link: Path, target: Path) -> None:
    if os.name != "nt":
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"directory symlink creation unavailable: {error}")
        return
    command = os.environ.get("COMSPEC")
    if not command:
        pytest.skip("COMSPEC is unavailable")
    result = subprocess.run(
        [command, "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
        shell=False,
    )
    if result.returncode != 0:
        pytest.skip("junction creation is unavailable")


def test_pinned_parent_link_cannot_escape_destructive_operations(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    active = repo / "active"
    active.mkdir(parents=True)
    (active / "file.txt").write_bytes(b"workspace")
    (active / "empty").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "file.txt"
    outside_file.write_bytes(b"outside sentinel")
    outside_empty = outside / "empty"
    outside_empty.mkdir()
    detached = repo / "detached"

    with SecureWorkspaceRoot(repo) as secure_root:
        active.rename(detached)
        _create_directory_link(active, outside)

        with pytest.raises(SecureFileError):
            secure_root.unlink_regular("active/file.txt", missing_ok=False)
        with pytest.raises(SecureFileError):
            secure_root.replace_regular("active/file.txt", b"replacement", mode=0o600)
        with pytest.raises(SecureFileError):
            secure_root.remove_directory("active/empty", missing_ok=False)

    assert outside_file.read_bytes() == b"outside sentinel"
    assert outside_empty.is_dir()
    assert (detached / "file.txt").read_bytes() == b"workspace"


def test_workspace_root_rejects_directory_link(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked-root"
    _create_directory_link(link, target)

    with pytest.raises(SecureFileError):
        SecureWorkspaceRoot(link)


def test_workspace_root_rejects_linked_parent(tmp_path: Path) -> None:
    target_parent = tmp_path / "target-parent"
    target_parent.mkdir()
    (target_parent / "repo").mkdir()
    linked_parent = tmp_path / "linked-parent"
    _create_directory_link(linked_parent, target_parent)

    with pytest.raises(
        SecureFileError,
        match=r"symbolic link|reparse point",
    ):
        SecureWorkspaceRoot(linked_parent / "repo")


def test_pinned_root_never_reanchors_to_replacement_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "inside.txt").write_bytes(b"pinned workspace")
    detached = tmp_path / "detached"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "inside.txt").write_bytes(b"outside sentinel")

    with SecureWorkspaceRoot(repo) as secure_root:
        repo.rename(detached)
        _create_directory_link(repo, outside)

        with pytest.raises(SecureFileError):
            secure_root.assert_root_identity()
        with (
            pytest.raises(SecureFileError),
            secure_root.open_binary("inside.txt"),
        ):
            pass

    assert (outside / "inside.txt").read_bytes() == b"outside sentinel"
    assert (detached / "inside.txt").read_bytes() == b"pinned workspace"


def test_destructive_operations_reject_replaced_hardlink(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "target.txt"
    target.write_bytes(b"workspace")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside sentinel")

    with SecureWorkspaceRoot(repo) as secure_root:
        target.unlink()
        os.link(outside, target)

        with pytest.raises(SecureFileError, match="single-link"):
            secure_root.unlink_regular("target.txt", missing_ok=False)
        with pytest.raises(SecureFileError, match="single-link"):
            secure_root.replace_regular("target.txt", b"replacement", mode=0o600)

    assert outside.read_bytes() == b"outside sentinel"


@pytest.mark.skipif(os.name == "nt", reason="POSIX root identity race")
def test_posix_root_open_rejects_path_swap_before_handle_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    alternate = tmp_path / "alternate"
    detached = tmp_path / "detached"
    repo.mkdir()
    alternate.mkdir()

    original_open = secure_file._open_posix_root_fd

    def swap_root_before_open(path: Path, *, expected: os.stat_result) -> int:
        repo.rename(detached)
        alternate.rename(repo)
        try:
            return original_open(path, expected=expected)
        finally:
            repo.rename(alternate)
            detached.rename(repo)

    monkeypatch.setattr(secure_file, "_open_posix_root_fd", swap_root_before_open)

    with pytest.raises(SecureFileError, match="identity changed during open"):
        SecureWorkspaceRoot(repo)


@pytest.mark.skipif(os.name != "nt", reason="Windows handle identity race")
def test_windows_replace_keeps_temporary_identity_bound() -> None:
    with tempfile.TemporaryDirectory(prefix="agent-hub-win-race-") as temporary_root:
        root = Path(temporary_root)
        repo = root / "repo"
        repo.mkdir()
        target = repo / "target.txt"
        target.write_bytes(b"before")
        external = root / "external.txt"
        external.write_bytes(b"external")

        mode = stat.S_IMODE(target.stat().st_mode)
        with SecureWorkspaceRoot(repo) as secure_root:
            secure_root.replace_regular("target.txt", b"after", mode=mode)

        assert target.read_bytes() == b"after"
        assert external.read_bytes() == b"external"
