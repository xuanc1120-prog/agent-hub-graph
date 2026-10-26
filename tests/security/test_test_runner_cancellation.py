from __future__ import annotations

import asyncio
import sys
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
    started: list[int] = []
    spawn = test_runner_module.asyncio.create_subprocess_exec

    async def record_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await spawn(*args, **kwargs)
        started.append(process.pid)
        return process

    monkeypatch.setattr(
        test_runner_module.asyncio,
        "create_subprocess_exec",
        record_spawn,
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

    assert len(started) == 1
    assert not psutil.pid_exists(started[0])


@pytest.mark.parametrize("boundary", ["spawn", "cleanup"])
async def test_repeated_cancellation_keeps_process_cleanup_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    entered = asyncio.Event()
    spawned = asyncio.Event()
    release = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []
    original_spawn = asyncio.create_subprocess_exec
    original_finish = test_runner_module._finish_process

    async def gated_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        spawned.set()
        if boundary == "spawn":
            entered.set()
            await release.wait()
        return process

    async def gated_finish(process, readers, job) -> None:
        if boundary == "cleanup":
            entered.set()
            await release.wait()
        await original_finish(process, readers, job)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", gated_spawn)
    monkeypatch.setattr(test_runner_module, "_finish_process", gated_finish)
    monkeypatch.setattr(
        SafeTestRunner,
        "_resolve",
        staticmethod(lambda _: (sys.executable, ("-c", "import time; time.sleep(30)"))),
    )
    runner = SafeTestRunner(CommandGuard(), timeout_seconds=60)
    running = asyncio.create_task(
        runner.run(["pytest"], repo=tmp_path, runtime_directory=tmp_path.parent / "runtime")
    )
    try:
        await asyncio.wait_for(spawned.wait(), timeout=10)
        if boundary == "cleanup":
            running.cancel()
        await asyncio.wait_for(entered.wait(), timeout=10)
        for _ in range(3):
            running.cancel()
            await asyncio.sleep(0)
        assert not running.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=15)
        assert len(processes) == 1
        process = processes[0]
        assert process.returncode is not None
        assert process._transport.is_closing()
        assert not psutil.pid_exists(process.pid)
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
            process._transport.close()
