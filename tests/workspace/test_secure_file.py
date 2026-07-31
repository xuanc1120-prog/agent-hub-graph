from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from workspace.secure_file import (
    SecureFileError,
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
