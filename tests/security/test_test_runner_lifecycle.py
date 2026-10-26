from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import psutil
import pytest

from app.config import Settings
from security.command_guard import CommandGuard
from security.test_runner import TestRunner as SafeTestRunner


def _helper_runner(
    monkeypatch: pytest.MonkeyPatch, script: str, *, limit: int, timeout: int = 10
) -> SafeTestRunner:
    monkeypatch.setattr(
        SafeTestRunner, "_resolve", staticmethod(lambda _: (sys.executable, ("-c", script)))
    )
    return SafeTestRunner(CommandGuard(), max_output_bytes=limit, timeout_seconds=timeout)


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.mark.parametrize(
    "mode", ["normal", "inherited_pipes", "error", "timeout", "cancel", "limit"]
)
async def test_runner_reaps_real_child_and_grandchild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    marker = tmp_path / "pids.json"
    child = (
        "import json, os, pathlib, subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)); "
        f"pathlib.Path({str(marker)!r}).write_text("
        "json.dumps([os.getppid(), os.getpid(), p.pid])); "
        "time.sleep(30)"
    )
    script = (
        "import pathlib, subprocess, sys, time\n"
        f"child = {child!r}\n"
        "subprocess.Popen([sys.executable, '-c', child], "
        + (
            ""
            if mode == "inherited_pipes"
            else "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
        )
        + "creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))\n"
        f"while not pathlib.Path({str(marker)!r}).exists(): time.sleep(0.01)\n"
        + {
            "normal": "sys.exit(0)",
            "inherited_pipes": "sys.exit(0)",
            "error": "sys.exit(7)",
            "timeout": "time.sleep(30)",
            "cancel": "time.sleep(30)",
            "limit": "sys.stdout.write('x' * 20000); sys.stdout.flush(); time.sleep(30)",
        }[mode]
    )
    runner = _helper_runner(monkeypatch, script, limit=1024, timeout=2 if mode == "timeout" else 10)
    task = asyncio.create_task(
        runner.run(
            ["pytest"],
            repo=tmp_path,
            runtime_directory=tmp_path.parent / "runtime",
        ),
    )
    pids: list[int] = []
    try:
        async with asyncio.timeout(10):
            while not marker.exists():
                if task.done():
                    await task
                await asyncio.sleep(0.01)
            # Child finishes its tiny JSON write before its parent can exit.
            while not pids:
                try:
                    pids = json.loads(marker.read_text())
                except json.JSONDecodeError:
                    await asyncio.sleep(0.01)
        if mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)
        else:
            result = await asyncio.wait_for(task, timeout=15)
            assert result.passed is (mode in {"normal", "inherited_pipes"})
            assert result.timed_out is (mode == "timeout")
            assert result.output_limit_exceeded is (mode == "limit")
        assert not any(_alive(pid) for pid in pids)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for pid in pids:
            if not _alive(pid):
                continue
            try:
                process = psutil.Process(pid)
                process.kill()
                process.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass


@pytest.mark.parametrize("size,exceeded", [(4096, False), (4097, True)])
async def test_raw_output_budget_is_shared_and_excludes_display_framing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int, exceeded: bool
) -> None:
    script = f"import os; os.write(1, b'x' * 2048); os.write(2, b'y' * {size - 2048})"
    runner = _helper_runner(monkeypatch, script, limit=4096)
    result = await runner.run(
        ["pytest"], repo=tmp_path, runtime_directory=tmp_path.parent / "runtime"
    )
    assert result.output_limit_exceeded is exceeded
    assert result.passed is not exceeded
    assert len(result.output.encode()) <= 4096


async def test_application_output_budget_accepts_more_than_one_million_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _helper_runner(
        monkeypatch,
        "import sys; sys.stdout.write('x' * 1000001)",
        limit=Settings().max_console_bytes_per_run,
    )
    result = await runner.run(
        ["pytest"], repo=tmp_path, runtime_directory=tmp_path.parent / "runtime"
    )
    assert result.passed
    assert len(result.output) > 1_000_000


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended-process assignment")
async def test_job_assignment_failure_never_executes_test_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from security.windows_job import WindowsJob

    marker = tmp_path / "executed"
    runner = _helper_runner(
        monkeypatch, f"from pathlib import Path; Path({str(marker)!r}).touch()", limit=1024
    )

    def reject(_self: WindowsJob, _pid: int) -> None:
        raise OSError("test assignment rejected")

    monkeypatch.setattr(WindowsJob, "assign", reject)
    with pytest.raises(OSError, match="test assignment rejected"):
        await runner.run(["pytest"], repo=tmp_path, runtime_directory=tmp_path.parent / "runtime")
    assert not marker.exists()
