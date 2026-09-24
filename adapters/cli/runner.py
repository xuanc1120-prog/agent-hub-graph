"""Safe, agent-agnostic CLI process transport.

The runner owns the process tree from spawn through final cleanup. On Windows
the root process is created suspended, assigned to a Job Object, and only then
resumed. Agent output is data only; it is never trusted as process identity.
POSIX process groups are a cleanup boundary, not an OS sandbox; descendants
that deliberately escape with ``setsid`` remain a HUB-330 isolation concern.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import signal
import subprocess
import sys
import time

import psutil

from adapters.cli.env import build_child_env
from adapters.cli.events import (
    MAX_EVENTS,
    MAX_TOTAL_OUTPUT_BYTES,
    CliEvent,
    CliRunErrorCode,
    CliRunResult,
)
from adapters.cli.execution_seal import ExecutionSeal
from adapters.cli.redact import sanitize_value
from adapters.cli.spec import CliAgentSpec, CliSpecError
from adapters.cli.streams import StreamAssembler
from adapters.cli.windows_job import WindowsJob, WindowsJobError

TERM_GRACE_SECONDS = 3.0
KILL_GRACE_SECONDS = 3.0
READ_CHUNK = 8_192
DRAIN_TIMEOUT_SECONDS = 10.0


class CliRunnerError(RuntimeError):
    """A fail-closed runner error that is not an Agent result."""


class _RunCancelled(Exception):
    """Internal signal used by the explicit ``cancel`` API."""


class _OutputBudget:
    """One shared byte budget for stdout and stderr."""

    __slots__ = ("exceeded", "limit", "truncated", "used")

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self.truncated = 0
        self.exceeded = False

    def consume(self, size: int) -> None:
        self.used += size
        if self.exceeded:
            self.truncated += size
        elif self.used > self.limit:
            self.truncated += self.used - self.limit
            self.exceeded = True


def spawn_kwargs(platform: str) -> dict[str, object]:
    """Return platform isolation flags without consulting ambient state."""

    if platform.startswith("win"):
        create_suspended = getattr(subprocess, "CREATE_SUSPENDED", 0x0000_0004)
        new_process_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x0000_0200)
        return {
            "creationflags": new_process_group | create_suspended,
        }
    return {"start_new_session": True}


async def _await_exit(proc: asyncio.subprocess.Process) -> int:
    """Poll the real return code without waiting for inherited pipe closure."""

    while proc.returncode is None:
        await asyncio.sleep(0.02)
    return proc.returncode


def _pgid_alive(pgid: int) -> bool:
    import os

    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_group_gone(pgid: int, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if not _pgid_alive(pgid):
            return True
        await asyncio.sleep(0.05)
    return not _pgid_alive(pgid)


async def _posix_group_stop(pgid: int) -> None:
    import os

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    if await _wait_group_gone(pgid, TERM_GRACE_SECONDS):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)
    await _wait_group_gone(pgid, KILL_GRACE_SECONDS)


def _psutil_tree_stop_sync(pid: int) -> None:
    """Best-effort root fallback when platform isolation setup failed."""

    try:
        root = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return
    try:
        victims = [*root.children(recursive=True), root]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return
    for process in victims:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            process.terminate()
    _, alive = psutil.wait_procs(victims, timeout=TERM_GRACE_SECONDS)
    for process in alive:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            process.kill()
    psutil.wait_procs(alive, timeout=KILL_GRACE_SECONDS)


class CliAgentRunner:
    """Run one immutable CLI spec while owning its complete process tree."""

    def __init__(
        self,
        spec: CliAgentSpec,
        *,
        timeout_seconds: float = 60.0,
        total_output_limit: int = MAX_TOTAL_OUTPUT_BYTES,
        drain_timeout_seconds: float = DRAIN_TIMEOUT_SECONDS,
        event_queue: asyncio.Queue[CliEvent] | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and greater than zero")
        if isinstance(total_output_limit, bool) or not isinstance(total_output_limit, int):
            raise ValueError("total_output_limit must be an integer")
        if not 1 <= total_output_limit <= MAX_TOTAL_OUTPUT_BYTES:
            raise ValueError(f"total_output_limit must be between 1 and {MAX_TOTAL_OUTPUT_BYTES}")
        if (
            isinstance(drain_timeout_seconds, bool)
            or not isinstance(drain_timeout_seconds, (int, float))
            or not math.isfinite(drain_timeout_seconds)
            or drain_timeout_seconds <= 0
        ):
            raise ValueError("drain_timeout_seconds must be finite and greater than zero")
        self._spec = spec
        self._timeout = timeout_seconds
        self._limit = total_output_limit
        self._drain_timeout = drain_timeout_seconds
        self._event_queue = event_queue
        self._proc: asyncio.subprocess.Process | None = None
        self._execution_seal: ExecutionSeal | None = None
        self._job: WindowsJob | None = None
        self._job_assigned = False
        self._cancel_event = asyncio.Event()
        self._events: list[CliEvent] = []
        self._dropped_events = 0
        self._seq = 0
        self._reap_lock = asyncio.Lock()
        self._running = False
        self._run_done: asyncio.Event | None = None
        self._assembler_stdout = self._make_assembler("stdout")
        self._assembler_stderr = self._make_assembler("stderr")

    async def run(self, *, extra_env: dict[str, str] | None = None) -> CliRunResult:
        """Execute the spec and return a bounded, redacted transport result."""

        if self._running:
            raise CliRunnerError("concurrent run not allowed")
        self._running = True
        run_done = asyncio.Event()
        self._run_done = run_done
        self._reset_run_state()
        started = time.monotonic()
        budget = _OutputBudget(self._limit)
        limit_hit = asyncio.Event()
        readers: list[asyncio.Task[None]] = []
        wait_task: asyncio.Task[int] | None = None
        cancel_task: asyncio.Task[bool] | None = None
        limit_task: asyncio.Task[bool] | None = None
        timed_out = False
        cancelled = False
        orphaned = False
        exit_code: int | None = None
        error_code = CliRunErrorCode.OK

        def terminal(code: CliRunErrorCode) -> CliRunResult:
            return CliRunResult(
                error_code=code,
                duration_seconds=time.monotonic() - started,
                events=tuple(self._events),
                dropped_event_count=self._dropped_events,
                truncated_output_bytes=budget.truncated,
            )

        def cancellation_result() -> CliRunResult:
            return CliRunResult(
                error_code=CliRunErrorCode.CANCELLED,
                cancelled=True,
                duration_seconds=time.monotonic() - started,
                events=tuple(self._events),
                dropped_event_count=self._dropped_events,
                truncated_output_bytes=budget.truncated,
            )

        try:
            if self._cancel_event.is_set():
                return cancellation_result()
            try:
                argv = self._spec.expanded_argv()
                env = build_child_env(extra_env)
            except ValueError:
                return terminal(CliRunErrorCode.SPAWN_FAILED)

            if self._cancel_event.is_set():
                return cancellation_result()
            try:
                self._execution_seal = ExecutionSeal.acquire(self._spec)
            except (CliSpecError, OSError):
                return terminal(CliRunErrorCode.IDENTITY_DRIFT)
            if not self._execution_seal.path_still_bound():
                return terminal(CliRunErrorCode.IDENTITY_DRIFT)

            if sys.platform.startswith("win"):
                try:
                    self._job = WindowsJob()
                except (OSError, WindowsJobError):
                    return terminal(CliRunErrorCode.PROCESS_ISOLATION)

            try:
                proc = await self._spawn(argv, env)
            except (OSError, ValueError):
                return terminal(CliRunErrorCode.SPAWN_FAILED)
            self._proc = proc

            if self._cancel_event.is_set():
                cancelled = True
                await self._shielded_reap()
                return cancellation_result()

            assert proc.stdout is not None
            assert proc.stderr is not None
            readers = [
                asyncio.create_task(self._pump(proc.stdout, "stdout", budget, limit_hit)),
                asyncio.create_task(self._pump(proc.stderr, "stderr", budget, limit_hit)),
            ]

            if sys.platform.startswith("win"):
                try:
                    assert self._job is not None
                    self._job.assign(proc.pid)
                    self._job_assigned = True
                    if self._cancel_event.is_set():
                        cancelled = True
                        await self._shielded_reap()
                        return cancellation_result()
                    seal = self._execution_seal
                    if seal is None or not seal.verify_spawned_process(proc.pid):
                        await self._shielded_reap()
                        return terminal(CliRunErrorCode.IDENTITY_DRIFT)
                    psutil.Process(proc.pid).resume()
                except (OSError, psutil.Error, WindowsJobError):
                    await self._shielded_reap()
                    return terminal(CliRunErrorCode.PROCESS_ISOLATION)

            seal = self._execution_seal
            if seal is not None:
                seal.close()
                self._execution_seal = None

            wait_task = asyncio.create_task(_await_exit(proc))
            cancel_task = asyncio.create_task(self._cancel_event.wait())
            limit_task = asyncio.create_task(limit_hit.wait())
            try:
                done, _ = await asyncio.wait(
                    {wait_task, cancel_task, limit_task},
                    timeout=self._timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done:
                    raise _RunCancelled
                if limit_task in done:
                    await self._shielded_reap()
                    exit_code = await self._bounded_returncode()
                    error_code = CliRunErrorCode.OUTPUT_LIMIT_EXCEEDED
                elif wait_task in done:
                    exit_code = wait_task.result()
                else:
                    raise TimeoutError
            except _RunCancelled:
                cancelled = True
                error_code = CliRunErrorCode.CANCELLED
                await self._shielded_reap()
            except TimeoutError:
                timed_out = True
                error_code = CliRunErrorCode.TIMED_OUT
                await self._shielded_reap()
            except asyncio.CancelledError:
                cancelled = True
                error_code = CliRunErrorCode.CANCELLED
                await self._shielded_reap()

            if timed_out or cancelled or error_code is CliRunErrorCode.OUTPUT_LIMIT_EXCEEDED:
                await self._cancel_tasks(readers)
            else:
                _, pending = await asyncio.wait(readers, timeout=self._drain_timeout)
                if pending:
                    if await self._has_survivors():
                        orphaned = True
                        await self._shielded_reap()
                    await self._cancel_tasks(list(pending))
                await asyncio.gather(*readers, return_exceptions=True)

            self._assembler_stdout.flush()
            self._assembler_stderr.flush()

            if not (timed_out or cancelled or error_code is CliRunErrorCode.OUTPUT_LIMIT_EXCEEDED):
                if await self._has_survivors():
                    orphaned = True
                    await self._shielded_reap()
                if orphaned:
                    error_code = CliRunErrorCode.ORPHANED_DESCENDANTS
                elif exit_code not in (None, 0):
                    error_code = CliRunErrorCode.EXIT_NON_ZERO
                else:
                    error_code = CliRunErrorCode.OK

            return CliRunResult(
                error_code=error_code,
                exit_code=exit_code if not (timed_out or cancelled) else None,
                timed_out=timed_out,
                cancelled=cancelled,
                exited=not timed_out and not cancelled,
                duration_seconds=time.monotonic() - started,
                events=tuple(self._events),
                dropped_event_count=self._dropped_events,
                truncated_output_bytes=budget.truncated,
            )
        except asyncio.CancelledError:
            await self._shielded_reap()
            return CliRunResult(
                error_code=CliRunErrorCode.CANCELLED,
                cancelled=True,
                duration_seconds=time.monotonic() - started,
                events=tuple(self._events),
                dropped_event_count=self._dropped_events,
                truncated_output_bytes=budget.truncated,
            )
        finally:
            try:
                await self._cancel_tasks(readers)
                await self._cancel_tasks(
                    [task for task in (wait_task, cancel_task, limit_task) if task is not None]
                )
                await self._shielded_reap()
            finally:
                seal = self._execution_seal
                if seal is not None:
                    seal.close()
                    self._execution_seal = None
                proc = self._proc
                if proc is not None:
                    with contextlib.suppress(Exception):
                        proc.transport.close()
                self._running = False
                run_done.set()
                if self._run_done is run_done:
                    self._run_done = None
                # A cancellation requested before ``run`` first receives a
                # timeslice belongs to this generation. Clear it only after
                # that generation has reached its terminal cleanup boundary.
                self._cancel_event.clear()

    async def cancel(self) -> None:
        """Request cancellation; the active ``run`` remains cleanup owner."""

        self._cancel_event.set()
        run_done = self._run_done
        if run_done is not None:
            await asyncio.shield(run_done.wait())

    async def _spawn(self, argv: list[str], env: dict[str, str]) -> asyncio.subprocess.Process:
        """Spawn without losing ownership if the caller is cancelled mid-create."""

        seal = self._execution_seal
        if seal is None:
            raise CliRunnerError("execution seal is not active")
        sealed_kwargs: dict[str, object] = {}
        program = seal.spawn_executable
        if seal.pass_fds:
            sealed_kwargs["pass_fds"] = seal.pass_fds
            # The kernel executes the immutable memfd, while argv[0] keeps the
            # registered path. Relocatable runtimes such as the Python builds
            # used by GitHub Actions derive their standard-library prefix from
            # argv[0] and otherwise fail when it is /proc/self/fd/<n>.
            sealed_kwargs["executable"] = seal.spawn_executable
            program = self._spec.executable
        task = asyncio.create_task(
            asyncio.create_subprocess_exec(
                program,
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self._spec.cwd,
                **sealed_kwargs,
                **spawn_kwargs(sys.platform),
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(task)
            if task.cancelled():
                raise
            self._proc = task.result()
            raise

    def _reset_run_state(self) -> None:
        self._events = []
        self._dropped_events = 0
        self._seq = 0
        self._proc = None
        self._execution_seal = None
        self._job = None
        self._job_assigned = False
        self._assembler_stdout = self._make_assembler("stdout")
        self._assembler_stderr = self._make_assembler("stderr")

    async def _shielded_reap(self) -> None:
        """Finish cleanup even when cancellation is repeated."""

        task = asyncio.create_task(self._reap())
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        await task

    async def _reap(self) -> None:
        async with self._reap_lock:
            proc = self._proc
            job = self._job
            job_assigned = self._job_assigned
            self._job = None
            self._job_assigned = False
            if sys.platform.startswith("win"):
                if job is not None:
                    if job_assigned:
                        try:
                            await asyncio.to_thread(job.terminate_and_close)
                        except (OSError, WindowsJobError) as exc:
                            if proc is not None:
                                await asyncio.to_thread(_psutil_tree_stop_sync, proc.pid)
                            raise CliRunnerError("windows job cleanup failed") from exc
                        return
                    await asyncio.to_thread(job.close_empty)
                if proc is not None and proc.returncode is None:
                    # Assignment never succeeded, so the suspended root is
                    # not a Job member and must be reclaimed by its own PID.
                    await asyncio.to_thread(_psutil_tree_stop_sync, proc.pid)
                return
            if proc is not None:
                await _posix_group_stop(proc.pid)

    async def _has_survivors(self) -> bool:
        proc = self._proc
        if proc is None:
            return False
        if sys.platform.startswith("win"):
            job = self._job
            if job is None:
                return proc.returncode is None
            try:
                return await asyncio.to_thread(job.has_active_processes)
            except (OSError, WindowsJobError):
                return True
        return _pgid_alive(proc.pid)

    async def _bounded_returncode(self) -> int | None:
        proc = self._proc
        if proc is None:
            return None
        deadline = asyncio.get_running_loop().time() + KILL_GRACE_SECONDS
        while proc.returncode is None and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        return proc.returncode

    @staticmethod
    async def _cancel_tasks(tasks: list[asyncio.Task]) -> None:
        if not tasks:
            return
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _emit(self, stream: str, payload: dict[str, object]) -> None:
        self._seq += 1
        if len(self._events) >= MAX_EVENTS:
            self._dropped_events += 1
            return
        event = CliEvent.create(
            seq=self._seq,
            stream=stream,
            payload=sanitize_value(payload),
        )
        self._events.append(event)
        if self._event_queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._event_queue.put_nowait(event)

    def _make_assembler(self, stream: str) -> StreamAssembler:
        def on_line(text: str) -> None:
            stripped = text.strip()
            if not stripped:
                return
            try:
                parsed = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                payload: dict[str, object] = {
                    "event_type": "cli.bad_line",
                    "chars": len(stripped),
                }
            else:
                payload = (
                    parsed
                    if isinstance(parsed, dict)
                    else {"event_type": "cli.json_non_object", "chars": len(stripped)}
                )
            self._emit(stream, payload)

        def on_oversized(total_bytes: int) -> None:
            self._emit(stream, {"event_type": "cli.line_oversized", "bytes": total_bytes})

        def on_decode_error(dropped: int) -> None:
            self._emit(stream, {"event_type": "cli.malformed_utf8", "dropped": dropped})

        return StreamAssembler(on_line, on_oversized, on_decode_error)

    async def _pump(
        self,
        reader: asyncio.StreamReader,
        stream: str,
        budget: _OutputBudget,
        limit_hit: asyncio.Event,
    ) -> None:
        assembler = self._assembler_stdout if stream == "stdout" else self._assembler_stderr
        while True:
            chunk = await reader.read(READ_CHUNK)
            if not chunk:
                return
            budget.consume(len(chunk))
            if budget.exceeded:
                limit_hit.set()
                return
            assembler.feed(chunk)


__all__ = ["CliAgentRunner", "CliRunnerError", "spawn_kwargs"]
