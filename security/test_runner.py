"""Bounded subprocess runner for Master-approved test argv vectors."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import psutil
from pydantic import Field

from protocol import FrozenStrictModel
from security.command_guard import ApprovedCommand, CommandGuard
from security.secret_policy import redact_secret_text

_REDACTION_OVERLAP_BYTES = 64 * 1024


class TestRunResult(FrozenStrictModel):
    template_id: str = Field(min_length=1, max_length=64)
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    exit_code: int | None = None
    timed_out: bool = False
    output: str = Field(max_length=1_000_000)
    output_truncated: bool = False
    duration_ms: int = Field(ge=0)

    @property
    def passed(self) -> bool:
        return not self.timed_out and self.exit_code == 0


@dataclass(slots=True)
class _OutputCollector:
    limit: int
    value: bytearray = field(default_factory=bytearray)
    truncated: bool = False

    async def read(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(16 * 1024):
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
            creationflags = getattr(__import__("subprocess"), "CREATE_NEW_PROCESS_GROUP", 0)
            creationflags |= getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)

        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
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
        assert process.stdout is not None
        assert process.stderr is not None
        capture_limit = self._max_output_bytes + _REDACTION_OVERLAP_BYTES
        stdout_collector = _OutputCollector(capture_limit)
        stderr_collector = _OutputCollector(capture_limit)
        readers = (
            asyncio.create_task(stdout_collector.read(process.stdout)),
            asyncio.create_task(stderr_collector.read(process.stderr)),
        )
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=self._timeout_seconds)
        except TimeoutError:
            timed_out = True
            await asyncio.to_thread(_terminate_process_tree, process.pid)
            await process.wait()
        except asyncio.CancelledError:
            await asyncio.to_thread(_terminate_process_tree, process.pid)
            await process.wait()
            raise
        finally:
            await asyncio.gather(*readers)

        stdout = _redact_output(stdout_collector.value.decode("utf-8", errors="replace"))
        stderr = _redact_output(stderr_collector.value.decode("utf-8", errors="replace"))
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
            output_truncated=(bounded or stdout_collector.truncated or stderr_collector.truncated),
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


def _terminate_process_tree(pid: int) -> None:
    if os.name != "nt":
        with suppress(OSError, ProcessLookupError):
            os.killpg(pid, signal.SIGTERM)
        time.sleep(2)
        with suppress(OSError, ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
        return
    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return
    processes = [*parent.children(recursive=True), parent]
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
