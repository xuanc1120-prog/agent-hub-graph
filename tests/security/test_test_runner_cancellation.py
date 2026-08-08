from __future__ import annotations

import asyncio
from pathlib import Path

import psutil
import pytest

from security import test_runner as test_runner_module
from security.command_guard import CommandGuard
from security.test_runner import TestRunner as SafeTestRunner


@pytest.mark.asyncio
async def test_runner_cancellation_terminates_process_and_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    tests = repo / "tests"
    tests.mkdir(parents=True)
    (tests / "test_cancel.py").write_text(
        "import time\n\ndef test_cancel():\n    time.sleep(30)\n",
        encoding="utf-8",
    )
    terminated: list[int] = []
    terminate = test_runner_module._terminate_process_tree

    def record_termination(pid: int) -> None:
        terminated.append(pid)
        terminate(pid)

    monkeypatch.setattr(
        test_runner_module,
        "_terminate_process_tree",
        record_termination,
    )
    runner = SafeTestRunner(CommandGuard(), timeout_seconds=60)
    running = asyncio.create_task(
        runner.run(
            ["pytest", "-q", "tests/test_cancel.py"],
            repo=repo,
            runtime_directory=tmp_path / "runtime",
        )
    )

    await asyncio.sleep(0.5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert len(terminated) == 1
    assert not psutil.pid_exists(terminated[0])
