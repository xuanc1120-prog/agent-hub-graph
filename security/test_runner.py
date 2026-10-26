"""Bounded subprocess runner for Master-approved test argv vectors."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

import psutil
from pydantic import Field

from protocol import FrozenStrictModel
from security.command_guard import ApprovedCommand, CommandGuard
from security.secret_policy import redact_secret_text
from security.windows_job import WindowsJob

_REDACTION_OVERLAP_BYTES = 64 * 1024
_T = TypeVar("_T")


class TestRunResult(FrozenStrictModel):
    template_id: str = Field(min_length=1, max_length=64)
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    exit_code: int | None = None
    timed_out: bool = False
    # The runner bounds UTF-8 bytes using its configured budget before construction.
    output: str
    output_truncated: bool = False
    output_limit_exceeded: bool = False
    duration_ms: int = Field(ge=0)

    @property
    def passed(self) -> bool:
        return not self.timed_out and not self.output_limit_exceeded and self.exit_code == 0


@dataclass(slots=True)
class _OutputBudget:
    limit: int
    used: int = 0
    exceeded: asyncio.Event = field(default_factory=asyncio.Event)

    def consume(self, size: int) -> None:
        self.used += size
        if self.used > self.limit:
            self.exceeded.set()


@dataclass(slots=True)
class _OutputCollector:
    limit: int
    budget: _OutputBudget | None = None
    value: bytearray = field(default_factory=bytearray)
    truncated: bool = False

    async def read(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(16 * 1024):
            if self.budget is not None:
                self.budget.consume(len(chunk))
            remaining = self.limit - len(self.value)
            if remaining <= 0:
                self.truncated = True
                continue
            self.value.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.truncated = True


class TestRunner:
    def __init__(
        self,
        command_guard: CommandGuard,
        *,
        timeout_seconds: int = 300,
        max_output_bytes: int = 1_000_000,
    ) -> None:
        if timeout_seconds < 1 or max_output_bytes < 1:
            raise ValueError("test runner limits must be positive")
        self._guard = command_guard
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes

    async def run(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        repo: Path,
        runtime_directory: Path,
    ) -> TestRunResult:
        approved = self._guard.validate(argv)
        root = repo.expanduser().resolve(strict=True)
        runtime = runtime_directory.expanduser().resolve(strict=False)
        try:
            common = Path(os.path.commonpath((root, runtime)))
        except ValueError:
            common = None
        if common == root:
            raise ValueError("test runtime directory must be outside the repository")
        runtime.mkdir(parents=True, exist_ok=True)
        executable, arguments = self._resolve(approved)
        environment = self._environment(runtime, executable, approved.template_id)
        creationflags = 0
        start_new_session = os.name != "nt"
        if os.name == "nt":
            # Suspend before assignment so a fast parent cannot spawn and exit first.
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | 0x4

        started = time.monotonic()
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                executable,
                *arguments,
                cwd=root,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                shell=False,
                creationflags=creationflags,
                start_new_session=start_new_session,
            )
        )
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await _finish_despite_cancellation(spawn)
            cleanup = asyncio.create_task(_finish_process(process, (), None))
            await _finish_despite_cancellation(cleanup)
            raise
        assert process.stdout is not None
        assert process.stderr is not None
        capture_limit = self._max_output_bytes + _REDACTION_OVERLAP_BYTES
        budget = _OutputBudget(self._max_output_bytes)
        stdout_collector = _OutputCollector(capture_limit, budget)
        stderr_collector = _OutputCollector(capture_limit, budget)
        readers = (
            asyncio.create_task(stdout_collector.read(process.stdout)),
            asyncio.create_task(stderr_collector.read(process.stderr)),
        )
        timed_out = False
        job: WindowsJob | None = None
        try:
            if os.name == "nt":
                job = WindowsJob()
                job.assign(process.pid)
                psutil.Process(process.pid).resume()
            await asyncio.wait_for(_wait_for_exit(process, budget), timeout=self._timeout_seconds)
        except TimeoutError:
            timed_out = True
        finally:
            cleanup = asyncio.create_task(_finish_process(process, readers, job))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await _finish_despite_cancellation(cleanup)
                raise

        exceeded = budget.exceeded.is_set()
        # A killed writer can leave a partial secret: don't publish its truncated raw prefix.
        stdout = (
            "Test output exceeded the configured byte budget."
            if exceeded
            else _redact_output(stdout_collector.value.decode("utf-8", errors="replace"))
        )
        stderr = (
            ""
            if exceeded
            else _redact_output(stderr_collector.value.decode("utf-8", errors="replace"))
        )
        framed = []
        if stdout:
            framed.append(f"[stdout] {stdout}")
        if stderr:
            framed.append(f"[stderr] {stderr}")
        output, bounded = _bounded_utf8("\n".join(framed), self._max_output_bytes)
        return TestRunResult(
            template_id=approved.template_id,
            argv=(executable, *arguments),
            exit_code=process.returncode,
            timed_out=timed_out,
            output=output,
            output_truncated=(
                exceeded or bounded or stdout_collector.truncated or stderr_collector.truncated
            ),
            output_limit_exceeded=exceeded,
            duration_ms=max(0, int((time.monotonic() - started) * 1_000)),
        )

    @staticmethod
    def _resolve(command: ApprovedCommand) -> tuple[str, tuple[str, ...]]:
        argv = command.argv
        executable = argv[0].casefold()
        if executable == "pytest":
            return sys.executable, ("-m", "pytest", *argv[1:])
        if executable in {"python", "python3"}:
            return sys.executable, argv[1:]
        resolved = shutil.which(argv[0])
        if resolved is None:
            raise FileNotFoundError(f"approved test executable is unavailable: {argv[0]}")
        launcher = Path(os.path.abspath(resolved))
        if not launcher.exists():
            raise FileNotFoundError(f"approved test executable is unavailable: {argv[0]}")
        return str(launcher), argv[1:]

    @staticmethod
    def _environment(
        runtime: Path,
        executable: str,
        template_id: str,
    ) -> dict[str, str]:
        executable_directories = [str(Path(executable).parent)]
        if template_id in {"npm-test", "pnpm-test"}:
            node = shutil.which("node")
            if node is None:
                raise FileNotFoundError(f"{template_id} requires an available Node.js executable")
            node_path = Path(os.path.abspath(node))
            if not node_path.exists():
                raise FileNotFoundError(f"{template_id} requires an available Node.js executable")
            node_directory = str(node_path.parent)
            if node_directory not in executable_directories:
                executable_directories.append(node_directory)
        environment = {
            "HOME": str(runtime),
            "PATH": os.pathsep.join(executable_directories),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_ADDOPTS": "-p no:cacheprovider",
            "TEMP": str(runtime),
            "TMP": str(runtime),
            "TMPDIR": str(runtime),
        }
        if os.name == "nt":
            for name in ("COMSPEC", "SYSTEMROOT", "WINDIR"):
                value = os.environ.get(name)
                if value:
                    environment[name] = value
        return environment


def _redact_output(value: str) -> str:
    return redact_secret_text(value)


def _bounded_utf8(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


async def _wait_for_exit(process: asyncio.subprocess.Process, budget: _OutputBudget) -> None:
    # Process.wait can wait for inherited pipes after the parent already exited.
    while process.returncode is None and not budget.exceeded.is_set():
        await asyncio.sleep(0.01)


async def _finish_despite_cancellation(task: asyncio.Task[_T]) -> _T:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _finish_process(
    process: asyncio.subprocess.Process,
    readers: tuple[asyncio.Task[None], ...],
    job: WindowsJob | None,
) -> None:
    try:
        if job is not None:
            try:
                await asyncio.to_thread(job.terminate)
            finally:
                # Also handles assignment failure while the root is still suspended.
                if process.returncode is None:
                    await asyncio.to_thread(_terminate_process_tree, process.pid)
        else:
            await asyncio.to_thread(_terminate_process_tree, process.pid)
        await asyncio.wait_for(process.wait(), timeout=5)
        await asyncio.wait_for(asyncio.gather(*readers), timeout=5)
    finally:
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        if job is not None:
            job.close()
        # asyncio.Process exposes its owned transport through _transport.
        # Close pipes even when drain/cleanup raises, before the loop goes away.
        process._transport.close()


def _terminate_process_tree(pid: int) -> None:
    if os.name != "nt":
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        time.sleep(2)
        with suppress(OSError, ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
        return
    try:
        parent = psutil.Process(pid)
        processes = [*parent.children(recursive=True), parent]
    except psutil.Error:
        return
    for process in reversed(processes):
        try:
            process.terminate()
        except psutil.Error:
            continue
    _, alive = psutil.wait_procs(processes, timeout=2)
    for process in alive:
        try:
            process.kill()
        except psutil.Error:
            continue
    psutil.wait_procs(alive, timeout=2)


__all__ = ["TestRunResult", "TestRunner"]
