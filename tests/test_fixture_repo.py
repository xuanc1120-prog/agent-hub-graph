import subprocess
from pathlib import Path


def test_fixture_source_repo_has_a_clean_commit(fixture_source_repo: Path) -> None:
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=fixture_source_repo,
        check=True,
        capture_output=True,
        text=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=fixture_source_repo,
        check=True,
        capture_output=True,
        text=True,
    )

    assert status.stdout == ""
    assert len(head.stdout.strip()) == 40
