from __future__ import annotations

import os
from pathlib import Path

import pytest

from security.command_guard import ApprovedCommand, CommandGuard
from security.test_runner import TestRunner as SafeTestRunner


@pytest.mark.asyncio
async def test_runner_uses_minimal_environment_and_bounded_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    tests = repo / "tests"
    tests.mkdir(parents=True)
    (tests / "test_environment.py").write_text(
        "import os\n\n"
        "def test_environment():\n"
        "    assert 'AGENT_HUB_DEMO_TOKEN' not in os.environ\n"
        "    print('x' * 2000)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_HUB_DEMO_TOKEN", "must-not-reach-child")
    runner = SafeTestRunner(CommandGuard(), timeout_seconds=30, max_output_bytes=64)

    result = await runner.run(
        ["pytest", "-q", "tests/test_environment.py"],
        repo=repo,
        runtime_directory=tmp_path / "runtime",
    )

    assert result.passed
    assert result.output_truncated
    assert "must-not-reach-child" not in result.output
    assert len(result.output.encode("utf-8")) <= 64


@pytest.mark.asyncio
async def test_runner_times_out_and_reaps_process_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    tests = repo / "tests"
    tests.mkdir(parents=True)
    (tests / "test_timeout.py").write_text(
        "import time\n\ndef test_timeout():\n    time.sleep(30)\n",
        encoding="utf-8",
    )
    runner = SafeTestRunner(CommandGuard(), timeout_seconds=1, max_output_bytes=10_000)

    result = await runner.run(
        ["pytest", "-q", "tests/test_timeout.py"],
        repo=repo,
        runtime_directory=tmp_path / "runtime",
    )

    assert result.timed_out
    assert not result.passed


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher symlink behavior")
def test_runner_preserves_package_manager_launcher_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "npm-cli.js"
    target.write_text("", encoding="utf-8")
    launcher = tmp_path / "npm"
    launcher.symlink_to(target)
    monkeypatch.setattr("security.test_runner.shutil.which", lambda _: str(launcher))

    executable, arguments = SafeTestRunner._resolve(ApprovedCommand("npm-test", ("npm", "test")))

    assert executable == str(launcher.absolute())
    assert arguments == ("test",)


def test_runner_adds_node_runtime_to_package_manager_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    npm_directory = tmp_path / "npm-bin"
    node_directory = tmp_path / "node-bin"
    npm_directory.mkdir()
    node_directory.mkdir()
    npm = npm_directory / ("npm.cmd" if os.name == "nt" else "npm")
    node = node_directory / ("node.exe" if os.name == "nt" else "node")
    npm.touch()
    node.touch()
    monkeypatch.setattr(
        "security.test_runner.shutil.which",
        lambda name: str(node) if name == "node" else None,
    )

    environment = SafeTestRunner._environment(
        tmp_path / "runtime",
        str(npm),
        "npm-test",
    )

    assert environment["PATH"].split(os.pathsep) == [
        str(npm_directory),
        str(node_directory),
    ]


def test_runner_rejects_package_manager_without_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("security.test_runner.shutil.which", lambda _: None)

    with pytest.raises(FileNotFoundError, match=r"requires an available Node\.js"):
        SafeTestRunner._environment(
            tmp_path / "runtime",
            str(tmp_path / "npm"),
            "npm-test",
        )
