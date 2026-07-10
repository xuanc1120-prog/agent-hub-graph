from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def fixture_source_repo(tmp_path: Path) -> Path:
    source = Path(__file__).parent / "fixtures" / "source_repo"
    repo = tmp_path / "source-repo"
    shutil.copytree(source, repo)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Agent Hub Tests",
            "-c",
            "user.email=tests@agent-hub.local",
            "commit",
            "-m",
            "fixture baseline",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo
