"""CliAgentRunner 行为测试: 全部基于本地无网络 helper CLI。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import psutil
import pytest
from helpers import EventCollector, payloads, types
from pydantic import ValidationError

from adapters.cli.events import MAX_EVENTS, CliRunErrorCode, CliRunResult
from adapters.cli.runner import CliAgentRunner, spawn_kwargs


async def run_simple(spec, *, timeout_seconds: float = 10.0) -> CliRunResult:
    runner = CliAgentRunner(spec, timeout_seconds=timeout_seconds)
    return await runner.run()


def dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _is_win() -> bool:
    return sys.platform.startswith("win")


async def _wait_pid_gone(pid: int, timeout: float = 6.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if not psutil.pid_exists(pid):
            return True
        await asyncio.sleep(0.05)
    return not psutil.pid_exists(pid)


class TestHappyPath:
    async def test_success_run_with_events(self, make_spec) -> None:
        collector = EventCollector()
        runner = CliAgentRunner(make_spec(["ok"]), timeout_seconds=10, event_queue=collector.queue)
        result = await runner.run()

        assert result.error_code is CliRunErrorCode.OK
        assert result.exited is True and result.exit_code == 0
        assert not result.timed_out and not result.cancelled
        assert "helper.started" in types(collector.items)
        assert "helper.done" in types(result.events)

    async def test_unknown_event_type_passes_through(self, make_spec) -> None:
        result = await run_simple(make_spec(["unknown"]))
        assert result.error_code is CliRunErrorCode.OK
        assert "totally.unknown_kind" in types(result.events)

    async def test_burst_output_tail_not_lost(self, make_spec) -> None:
        # 复审回归: 正常退出路径必须先有界排空管道再收尾。
        result = await run_simple(make_spec(["burstexit"]))
        kinds = types(result.events)
        assert kinds.count("helper.burst") == 200
        assert kinds[-1] == "helper.burst_done"
        assert result.error_code is CliRunErrorCode.OK

    async def test_event_cap_no_async_race(self, make_spec) -> None:
        # 复审回归: 突发 2000 条事件的槽位必须在同步段预留。
        from adapters.cli.events import MAX_EVENTS

        runner = CliAgentRunner(make_spec(["ok"]), timeout_seconds=10)
        runner._reset_run_state()
        for i in range(2000):
            runner._emit("stdout", {"event_type": "cli.burst", "i": i})
        assert len(runner._events) == MAX_EVENTS
        assert runner._dropped_events == 2000 - MAX_EVENTS

    async def test_oversized_expanded_prompt_maps_to_spawn_failed(self, make_spec) -> None:
        # 复审回归: 展开后重校验失败必须返回稳定错误码而非异常。
        spec = make_spec(["echoprompt", "{prompt}"], prompt="\u6d4b" * 1500)
        result = await run_simple(spec)
        assert result.error_code is CliRunErrorCode.SPAWN_FAILED
        assert result.exit_code is None

    async def test_prompt_substitution_single_token(self, make_spec) -> None:
        # prompt 含空格与特殊字符,也必须只停留在同一个 argv token 内。
        spec = make_spec(
            ["echoprompt", "{prompt}"],
            prompt="fix bug; rm -rf {x} $HOME",
        )
        result = await run_simple(spec)
        got = [p for p in payloads(result.events) if p.get("event_type") == "helper.prompt"]
        assert len(got) == 1
        assert got[0]["value"] == "fix bug; rm -rf {x} $HOME"
        assert result.error_code is CliRunErrorCode.OK


class TestStreamSemantics:
    async def test_fragmented_jsonl_parsed_incrementally(self, make_spec) -> None:
        collector = EventCollector()
        runner = CliAgentRunner(
            make_spec(["frag"]), timeout_seconds=10, event_queue=collector.queue
        )
        result = await runner.run()

        assert result.error_code is CliRunErrorCode.OK
        frag = [p for p in payloads(collector.items) if p.get("event_type") == "helper.frag"]
        assert len(frag) == 1
        # 跨 chunk 的多字节字符必须完整还原。
        assert "\U0001f600" in str(frag[0].get("note"))

    async def test_bad_line_then_continue_no_raw_leak(self, make_spec) -> None:
        result = await run_simple(make_spec(["badline"]))
        assert types(result.events) == [
            "helper.before",
            "cli.bad_line",
            "helper.after",
        ]
        bad = [p for p in payloads(result.events) if p["event_type"] == "cli.bad_line"]
        assert set(bad[0].keys()) == {"event_type", "chars"}
        assert "TOP_SECRET_VALUE" not in dump(payloads(result.events))

    async def test_invalid_utf8_recovers(self, make_spec) -> None:
        result = await run_simple(make_spec(["badutf8"]))
        kinds = types(result.events)
        assert "cli.malformed_utf8" in kinds
        assert "helper.after_utf8" in kinds
        assert result.error_code is CliRunErrorCode.OK

    async def test_oversized_line_bounded_and_continues(self, make_spec) -> None:
        result = await run_simple(make_spec(["longline"]))
        oversized = [p for p in payloads(result.events) if p["event_type"] == "cli.line_oversized"]
        assert len(oversized) == 1
        assert "helper.after_long" in types(result.events)
        assert "xxxxxxx" not in dump(payloads(result.events))


class TestRedactionBoundary:
    @pytest.mark.parametrize("mode", ["secret", "badline"])
    async def test_secrets_never_surface(self, make_spec, mode: str) -> None:
        collector = EventCollector()
        runner = CliAgentRunner(make_spec([mode]), timeout_seconds=10, event_queue=collector.queue)
        result = await runner.run()

        for source in (payloads(result.events), payloads(collector.items)):
            text = dump(source)
            assert "SECRET_VALUE_123" not in text
            assert "TOP_SECRET_VALUE" not in text
        if mode == "secret":
            rows = [p for p in payloads(result.events) if p.get("event_type") == "helper.secret"]
            assert rows and "[REDACTED]" in str(rows[0])

    async def test_fixed_credentials_redacted_across_chunks(self, make_spec) -> None:
        collector = EventCollector()
        runner = CliAgentRunner(
            make_spec(["fixedsecrets"]),
            timeout_seconds=10,
            event_queue=collector.queue,
        )
        result = await runner.run()

        assert result.error_code is CliRunErrorCode.OK
        for events in (result.events, tuple(collector.items)):
            text = dump(payloads(events))
            for leaked in (
                "AKIAIOSFODNN7EXAMPLE",
                "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
                "signature1234",
                "synthetic-password",
                "QUJDREVGR0g=",
            ):
                assert leaked not in text
            assert "[REDACTED]" in text

    async def test_composite_credential_keys_redacted_in_result_and_queue(self, make_spec) -> None:
        collector = EventCollector()
        runner = CliAgentRunner(
            make_spec(["compositekeys"]),
            timeout_seconds=10,
            event_queue=collector.queue,
        )
        result = await runner.run()

        assert result.error_code is CliRunErrorCode.OK
        secrets = (
            "opaque-password-value",
            "opaque-token-value",
            "opaque-api-value",
            "opaque-primary-value",
        )
        result_text = dump(payloads(result.events))
        queue_text = dump(payloads(collector.items))
        assert all(secret not in result_text for secret in secrets)
        assert all(secret not in queue_text for secret in secrets)


class TestLifecycleErrors:
    async def test_non_zero_exit_code(self, make_spec) -> None:
        result = await run_simple(make_spec(["fail", "7"]))
        assert result.error_code is CliRunErrorCode.EXIT_NON_ZERO
        assert result.exit_code == 7
        assert result.exited is True

    async def test_timeout_returns_stable_code(self, make_spec) -> None:
        result = await run_simple(make_spec(["hang"]), timeout_seconds=1)
        assert result.error_code is CliRunErrorCode.TIMED_OUT
        assert result.timed_out is True and result.exited is False
        assert result.duration_seconds < 15

    async def test_explicit_cancel_returns_cancelled(self, make_spec) -> None:
        runner = CliAgentRunner(make_spec(["hang"]), timeout_seconds=30)
        run_task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.4)
        await runner.cancel()
        result = await run_task

        assert result.error_code is CliRunErrorCode.CANCELLED
        assert result.cancelled is True
        assert not result.timed_out and not result.exited

    async def test_cancel_before_run_is_scheduled_prevents_side_effect(
        self, make_spec, tmp_path
    ) -> None:
        marker = tmp_path / "must-not-run.txt"
        runner = CliAgentRunner(make_spec(["touch", str(marker)]), timeout_seconds=10)

        task = asyncio.create_task(runner.run())
        await runner.cancel()
        result = await task

        assert result.error_code is CliRunErrorCode.CANCELLED
        assert result.cancelled is True
        assert result.timed_out is False
        assert result.exited is False
        assert marker.exists() is False


class TestProcessTreeReap:
    async def test_grandchild_reaped_on_timeout(self, make_spec) -> None:
        collector = EventCollector()
        runner = CliAgentRunner(
            make_spec(["hang", "--tree"]),
            timeout_seconds=1,
            event_queue=collector.queue,
        )
        result = await runner.run()

        assert result.error_code is CliRunErrorCode.TIMED_OUT
        pids = [
            int(p["pid"])
            for p in payloads(collector.items)
            if p.get("event_type") == "helper.grandchild"
        ]
        assert len(pids) == 1
        assert await _wait_pid_gone(pids[0]) is True

    async def test_orphan_grandchild_after_parent_zero_exit(self, make_spec) -> None:
        # 复审回归: 父进程退出 0 但孙进程持管道长眠 -> 必须回收整树,
        # 返回稳定 ORPHANED_DESCENDANTS 而非 OK。
        collector = EventCollector()
        runner = CliAgentRunner(
            make_spec(["orphanexit", "--tree"]),
            timeout_seconds=10,
            drain_timeout_seconds=1.5,
            event_queue=collector.queue,
        )
        result = await runner.run()

        assert result.error_code is CliRunErrorCode.ORPHANED_DESCENDANTS
        assert result.exited is True and result.exit_code == 0
        pids = [
            int(p["pid"])
            for p in payloads(collector.items)
            if p.get("event_type") == "helper.grandchild"
        ]
        assert len(pids) == 1
        assert await _wait_pid_gone(pids[0]) is True

    async def test_orphan_grandchild_preserves_nonzero_parent_exit(self, make_spec) -> None:
        collector = EventCollector()
        runner = CliAgentRunner(
            make_spec(["orphanfail", "7"]),
            timeout_seconds=10,
            drain_timeout_seconds=1.5,
            event_queue=collector.queue,
        )
        result = await runner.run()

        assert result.error_code is CliRunErrorCode.ORPHANED_DESCENDANTS
        assert result.exited is True and result.exit_code == 7
        pids = [
            int(item["pid"])
            for item in payloads(collector.items)
            if item.get("event_type") == "helper.grandchild"
        ]
        assert len(pids) == 1
        assert await _wait_pid_gone(pids[0]) is True

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX 进程组语义")
    async def test_sigterm_ignoring_tree_killed_on_timeout_posix(self, make_spec) -> None:
        # 复审回归: 父子孙全部忽略 SIGTERM 时必须升级到 SIGKILL,
        # 且以 killpg(pgid, 0) 的组级探活为准。
        collector = EventCollector()
        runner = CliAgentRunner(
            make_spec(["hangignore", "--tree"]),
            timeout_seconds=1,
            event_queue=collector.queue,
        )
        result = await runner.run()

        assert result.error_code is CliRunErrorCode.TIMED_OUT
        pids = [
            int(p["pid"])
            for p in payloads(collector.items)
            if p.get("event_type") == "helper.grandchild"
        ]
        assert len(pids) == 1
        assert await _wait_pid_gone(pids[0]) is True

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX 进程组语义")
    async def test_sigterm_ignoring_tree_killed_on_cancel_posix(self, make_spec) -> None:
        collector = EventCollector()
        runner = CliAgentRunner(
            make_spec(["hangignore", "--tree"]),
            timeout_seconds=30,
            event_queue=collector.queue,
        )
        run_task = asyncio.create_task(runner.run())
        await asyncio.sleep(1.0)
        await runner.cancel()
        result = await run_task

        assert result.error_code is CliRunErrorCode.CANCELLED
        pids = [
            int(p["pid"])
            for p in payloads(collector.items)
            if p.get("event_type") == "helper.grandchild"
        ]
        assert len(pids) == 1
        assert await _wait_pid_gone(pids[0]) is True


async def test_total_output_limit_triggers_stable_code(make_spec) -> None:
    runner = CliAgentRunner(make_spec(["flood"]), timeout_seconds=10, total_output_limit=64_000)
    result = await runner.run()

    assert result.error_code is CliRunErrorCode.OUTPUT_LIMIT_EXCEEDED
    assert result.truncated_output_bytes > 0
    assert len(result.events) <= 50


class TestEnvIsolationEndToEnd:
    async def test_child_sees_whitelist_only(self, make_spec, monkeypatch) -> None:
        monkeypatch.setenv("AGENT_HUB_TOKEN", "must-not-leak")
        names = ",".join(["AGENT_HUB_TOKEN", "SystemRoot", "PATH"])
        result = await run_simple(make_spec(["envdump", names]))

        dumps = [p for p in payloads(result.events) if p.get("event_type") == "helper.env"]
        assert dumps
        present = dumps[-1]["present"]
        assert isinstance(present, list)
        assert "AGENT_HUB_TOKEN" not in present
        if _is_win():
            assert "SystemRoot" in present

    async def test_extra_env_override_within_whitelist(self, make_spec) -> None:
        from adapters.cli import env as env_mod

        key = env_mod.allowed_keys()[0]
        marker = f"hub300-{key}-marker"
        runner = CliAgentRunner(make_spec(["envdump", key]), timeout_seconds=10)
        result = await runner.run(extra_env={key: marker})

        dumps = [p for p in payloads(result.events) if p.get("event_type") == "helper.env"]
        assert dumps and key in dumps[-1]["present"]


class TestResultModelInvariants:
    def test_flag_conflicts_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CliRunResult(error_code=CliRunErrorCode.OK, timed_out=True, cancelled=True)
        with pytest.raises(ValidationError):
            CliRunResult(error_code=CliRunErrorCode.OK, exited=True, timed_out=True)

    def test_error_code_flag_binding(self) -> None:
        with pytest.raises(ValidationError):
            CliRunResult(error_code=CliRunErrorCode.TIMED_OUT, timed_out=False)
        with pytest.raises(ValidationError):
            CliRunResult(error_code=CliRunErrorCode.CANCELLED, cancelled=False)
        with pytest.raises(ValidationError):
            CliRunResult(
                error_code=CliRunErrorCode.TIMED_OUT,
                timed_out=True,
                cancelled=True,
            )

    def test_ok_and_exit_bindings(self) -> None:
        # 合法基线: OK 必须是 exited + exit_code == 0。
        ok = CliRunResult(error_code=CliRunErrorCode.OK, exited=True, exit_code=0)
        assert ok.exited is True
        with pytest.raises(ValidationError):
            CliRunResult(error_code=CliRunErrorCode.OK)  # 未 exited
        with pytest.raises(ValidationError):
            CliRunResult(error_code=CliRunErrorCode.OK, exited=True)  # exit 缺失
        with pytest.raises(ValidationError):
            CliRunResult(error_code=CliRunErrorCode.EXIT_NON_ZERO, exited=True, exit_code=0)
        with pytest.raises(ValidationError):
            CliRunResult(
                error_code=CliRunErrorCode.OUTPUT_LIMIT_EXCEEDED, exited=True
            )  # exit_code 缺失

    def test_spawn_failed_has_no_flags(self) -> None:
        result = CliRunResult(error_code=CliRunErrorCode.SPAWN_FAILED)
        assert not (result.timed_out or result.cancelled or result.exited)
        assert result.exit_code is None

    def test_orphaned_descendants_preserves_nonzero_exit(self) -> None:
        result = CliRunResult(
            error_code=CliRunErrorCode.ORPHANED_DESCENDANTS,
            exited=True,
            exit_code=7,
        )
        assert result.exit_code == 7

    def test_frozen_models_reject_assignment(self) -> None:
        # 复审回归: 冻结模型赋值必须立即抛错且不残留非法状态。
        result = CliRunResult(error_code=CliRunErrorCode.OK, exited=True, exit_code=0)
        with pytest.raises(ValidationError):
            result.timed_out = True
        assert result.timed_out is False
        assert result.error_code is CliRunErrorCode.OK
        assert result.exited is True and result.exit_code == 0

    def test_events_tuple_cannot_grow(self) -> None:
        # 复审回归: 结果事件集合为 tuple, 无法 append 扩容。
        from adapters.cli.events import CliEvent

        result = CliRunResult(
            error_code=CliRunErrorCode.OK,
            exited=True,
            exit_code=0,
            events=(CliEvent.create(seq=1, stream="stdout", payload={"a": 1}),),
        )
        assert isinstance(result.events, tuple)
        with pytest.raises(AttributeError):
            result.events.append(  # type: ignore[attr-defined]
                CliEvent.create(seq=2, stream="stdout", payload={"b": 2})
            )
        assert len(result.events) == 1

    def test_direct_huge_payload_json_rejected(self) -> None:
        # 复审回归: 直构非法 JSON / 非对象负载必须被 ValidationError 拒绝;
        # 合法但超大的对象会被幂等脱敏+限长降级到 MAX_EVENT_BYTES 内,
        # 保证任何构造路径下事件都有确定上限。
        from adapters.cli.events import MAX_EVENT_BYTES, CliEvent

        with pytest.raises(ValidationError):
            CliEvent(seq=1, stream="stdout", payload_json="{not-json")
        with pytest.raises(ValidationError):
            CliEvent(seq=1, stream="stdout", payload_json="[1, 2, 3]")
        with pytest.raises(ValidationError):
            CliEvent(seq=1, stream="stdout", payload_json='{"a": 1')
        huge = json.dumps({"k": "v" * 100_000})
        event = CliEvent(seq=1, stream="stdout", payload_json=huge)
        stored = len(event.payload_json.encode("utf-8"))
        assert stored <= MAX_EVENT_BYTES
        # 单一超大字符串值被限长截断, 结构保留。
        assert len(event.payload["k"]) <= 2_100
        # 整体超限(多字段)才降级为占位负载。
        wide = json.dumps({f"k{i}": "v" * 3_000 for i in range(50)})
        wide_event = CliEvent(seq=2, stream="stderr", payload_json=wide)
        assert wide_event.payload["event_type"] == "cli.event_oversized"
        # 合法小对象可直构, 且会被规范化脱敏。
        event = CliEvent(seq=1, stream="stdout", payload_json='{"token": "x"}')
        assert event.payload["token"] == "[REDACTED]"

    def test_event_payload_deeply_immutable(self) -> None:
        from adapters.cli.events import CliEvent

        event = CliEvent.create(
            seq=1, stream="stdout", payload={"event_type": "t", "nested": {"a": 1}}
        )
        snapshot = event.payload_json
        # 通过属性拿到的副本被修改, 不影响存储状态。
        mutated = event.payload
        mutated["nested"]["a"] = 999
        mutated["injected"] = True
        assert event.payload_json == snapshot
        assert event.payload["nested"]["a"] == 1
        assert "injected" not in event.payload

    def test_max_events_constant_sane(self) -> None:
        assert MAX_EVENTS >= 100


class TestSpawnKwargsPureFunction:
    async def test_fd_seal_executes_copy_but_preserves_registered_argv0(
        self, make_spec, monkeypatch
    ) -> None:
        spec = make_spec(["ok"])
        runner = CliAgentRunner(spec)
        sealed_path = "/proc/self/fd/91"
        runner._execution_seal = SimpleNamespace(
            spawn_executable=sealed_path,
            pass_fds=(91,),
        )
        captured: dict[str, object] = {}
        expected = object()

        async def fake_create(*args: str, **kwargs: object):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return expected

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

        actual = await runner._spawn(["helper.py", "ok"], {})

        assert actual is expected
        assert captured["args"] == (spec.executable, "helper.py", "ok")
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["executable"] == sealed_path
        assert kwargs["pass_fds"] == (91,)

    def test_windows_branch(self) -> None:
        kwargs = spawn_kwargs("win32")
        assert "creationflags" in kwargs
        assert int(kwargs["creationflags"]) & getattr(subprocess, "CREATE_SUSPENDED", 0x4)
        assert "start_new_session" not in kwargs

    def test_posix_branch(self) -> None:
        kwargs = spawn_kwargs("linux")
        assert kwargs.get("start_new_session") is True
        assert "creationflags" not in kwargs


class TestStringKvEndToEnd:
    async def test_kvleak_nested_unknown_callback_redacted(self, make_spec) -> None:
        # P1-4 回归: {"message": "..."} 中的复合凭据必须脱敏,
        # 覆盖嵌套 message、未知 event 与 callback 三通道。
        collector = EventCollector()
        runner = CliAgentRunner(
            make_spec(["kvleak"]), timeout_seconds=10, event_queue=collector.queue
        )
        result = await runner.run()

        assert result.error_code is CliRunErrorCode.OK
        for source in (payloads(result.events), payloads(collector.items)):
            text = dump(source)
            assert "OUTSIDE_SECRET" not in text
            assert "secret123" not in text
            assert "secret_access_key" in text or "DB_PASS" in text or "message" in text
        nested = [p for p in payloads(result.events) if p.get("event_type") == "helper.nested"]
        assert nested and "secret123" not in dump(nested)
        unknown = [
            p for p in payloads(result.events) if p.get("event_type") == "totally.unknown_kind"
        ]
        assert unknown and "secret123" not in dump(unknown)
        kept = [p for p in payloads(result.events) if p.get("message") == "primary_key=id_pk"]
        assert kept


class TestJobIsolationFailClosed:
    @pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows Job 语义")
    async def test_job_precreate_failure_no_spawn(self, make_spec, monkeypatch) -> None:
        import adapters.cli.runner as runner_mod

        def fail_job() -> None:
            raise runner_mod.WindowsJobError("injected")

        monkeypatch.setattr(runner_mod, "WindowsJob", fail_job)
        runner = CliAgentRunner(make_spec(["ok"]), timeout_seconds=10)
        result = await runner.run()

        assert result.error_code is CliRunErrorCode.PROCESS_ISOLATION
        assert result.error_code is not CliRunErrorCode.OK
        assert runner._proc is None

    @pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows Job 语义")
    async def test_job_assign_failure_never_executes_agent(
        self, make_spec, monkeypatch, tmp_path
    ) -> None:
        # The process is still suspended when assignment fails. No Agent
        # instruction may execute, and the suspended root must be reaped.
        import adapters.cli.runner as runner_mod

        marker = tmp_path / "must-not-exist.txt"

        def fail_assign(self: object, pid: int) -> None:
            del self, pid
            raise runner_mod.WindowsJobError("injected")

        monkeypatch.setattr(runner_mod.WindowsJob, "assign", fail_assign)
        runner = CliAgentRunner(make_spec(["touch", str(marker)]), timeout_seconds=10)
        result = await runner.run()

        assert result.error_code is CliRunErrorCode.PROCESS_ISOLATION
        assert marker.exists() is False
        assert runner._proc is not None
        assert await _wait_pid_gone(runner._proc.pid) is True

    def test_process_isolation_result_invariants(self) -> None:
        result = CliRunResult(error_code=CliRunErrorCode.PROCESS_ISOLATION)
        assert not (result.timed_out or result.cancelled or result.exited)
        assert result.exit_code is None

    async def test_agent_event_pid_is_never_process_authority(self, make_spec) -> None:
        current_pid = psutil.Process().pid
        result = await run_simple(make_spec(["emitpid", str(current_pid)]))

        assert result.error_code is CliRunErrorCode.OK
        assert psutil.pid_exists(current_pid)

    @pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows Job 语义")
    async def test_job_owner_terminates_and_closes_once(self, make_spec, monkeypatch) -> None:
        import adapters.cli.runner as runner_mod

        instances: list[object] = []

        class FakeJob:
            def __init__(self) -> None:
                self.terminate_calls = 0
                instances.append(self)

            def assign(self, pid: int) -> None:
                assert pid > 0

            def has_active_processes(self) -> bool:
                return False

            def terminate_and_close(self) -> None:
                self.terminate_calls += 1

        monkeypatch.setattr(runner_mod, "WindowsJob", FakeJob)
        result = await run_simple(make_spec(["ok"]))

        assert result.error_code is CliRunErrorCode.OK
        assert len(instances) == 1
        assert instances[0].terminate_calls == 1


class TestRunLifecycleCleanup:
    @pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows Job 建立语义")
    async def test_cancel_while_spawn_pending_keeps_run_as_job_owner(
        self, make_spec, monkeypatch
    ) -> None:
        runner = CliAgentRunner(make_spec(["hang"]), timeout_seconds=30)
        original_spawn = runner._spawn
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_spawn(argv: list[str], env: dict[str, str]):
            entered.set()
            await release.wait()
            return await original_spawn(argv, env)

        monkeypatch.setattr(runner, "_spawn", delayed_spawn)
        run_task = asyncio.create_task(runner.run())
        await entered.wait()
        assert runner._job is not None
        cancel_tasks = [asyncio.create_task(runner.cancel()) for _ in range(2)]
        await asyncio.sleep(0)
        assert all(not task.done() for task in cancel_tasks)
        release.set()

        result = await run_task
        await asyncio.gather(*cancel_tasks)
        assert result.error_code is CliRunErrorCode.CANCELLED
        assert runner._job is None

    @pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows Job 建立语义")
    async def test_cancel_after_spawn_before_assign_reaps_suspended_root(
        self, make_spec, monkeypatch
    ) -> None:
        runner = CliAgentRunner(make_spec(["hang"]), timeout_seconds=30)
        original_spawn = runner._spawn
        spawned = asyncio.Event()
        release = asyncio.Event()
        process = None

        async def delayed_return(argv: list[str], env: dict[str, str]):
            nonlocal process
            process = await original_spawn(argv, env)
            spawned.set()
            await release.wait()
            return process

        monkeypatch.setattr(runner, "_spawn", delayed_return)
        run_task = asyncio.create_task(runner.run())
        await spawned.wait()
        cancel_task = asyncio.create_task(runner.cancel())
        await asyncio.sleep(0)
        assert cancel_task.done() is False
        release.set()

        result = await run_task
        await cancel_task
        assert result.error_code is CliRunErrorCode.CANCELLED
        assert process is not None
        assert await _wait_pid_gone(process.pid) is True

    @pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows Job 建立语义")
    async def test_cancel_requested_during_assign_prevents_resume(
        self, make_spec, monkeypatch, tmp_path
    ) -> None:
        import adapters.cli.runner as runner_mod

        marker = tmp_path / "must-not-run.txt"
        original_assign = runner_mod.WindowsJob.assign
        runner = CliAgentRunner(make_spec(["touch", str(marker)]), timeout_seconds=10)

        def assign_and_cancel(job: object, pid: int) -> None:
            original_assign(job, pid)
            runner._cancel_event.set()

        monkeypatch.setattr(runner_mod.WindowsJob, "assign", assign_and_cancel)
        result = await runner.run()

        assert result.error_code is CliRunErrorCode.CANCELLED
        assert marker.exists() is False

    async def test_prespawn_failure_has_no_process(self, make_spec) -> None:
        collector = EventCollector()
        spec = make_spec(["echoprompt", "{prompt}"], prompt="\u6d4b" * 1500)
        runner = CliAgentRunner(spec, timeout_seconds=10, event_queue=collector.queue)
        result = await runner.run()

        assert result.error_code is CliRunErrorCode.SPAWN_FAILED
        assert runner._proc is None
        assert runner._running is False

    async def test_full_event_queue_never_blocks_runner(self, make_spec) -> None:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        await queue.put("already-full")
        runner = CliAgentRunner(make_spec(["ok"]), timeout_seconds=10, event_queue=queue)
        result = await asyncio.wait_for(runner.run(), timeout=15)

        assert result.error_code is CliRunErrorCode.OK
        assert queue.qsize() == 1

    async def test_no_external_event_queue(self, make_spec) -> None:
        runner = CliAgentRunner(make_spec(["ok"]), timeout_seconds=10, event_queue=None)
        result = await runner.run()

        assert result.error_code is CliRunErrorCode.OK
        assert runner._running is False

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"timeout_seconds": 0}, "timeout_seconds"),
            ({"timeout_seconds": float("inf")}, "timeout_seconds"),
            ({"timeout_seconds": True}, "timeout_seconds"),
            ({"total_output_limit": 0}, "total_output_limit"),
            ({"total_output_limit": True}, "total_output_limit"),
            ({"total_output_limit": MAX_EVENTS * MAX_EVENTS + 1}, "total_output_limit"),
            ({"drain_timeout_seconds": float("nan")}, "drain_timeout_seconds"),
            ({"drain_timeout_seconds": False}, "drain_timeout_seconds"),
        ],
    )
    def test_invalid_runtime_limits_rejected(self, make_spec, kwargs, message) -> None:
        with pytest.raises(ValueError, match=message):
            CliAgentRunner(make_spec(["ok"]), **kwargs)

    async def test_concurrent_run_rejected(self, make_spec) -> None:
        # P1-3 回归: 同一 runner 并发 run() 必须明确拒绝。
        from adapters.cli.runner import CliRunnerError

        runner = CliAgentRunner(make_spec(["hang"]), timeout_seconds=10)
        first = asyncio.create_task(runner.run())
        await asyncio.sleep(0.5)
        with pytest.raises(CliRunnerError, match="concurrent"):
            await runner.run()
        await runner.cancel()
        result = await first
        assert result.error_code is CliRunErrorCode.CANCELLED

    async def test_repeated_cancel_idempotent(self, make_spec) -> None:
        # P1-3 回归: 清理必须承受重复取消。
        runner = CliAgentRunner(make_spec(["hang"]), timeout_seconds=30)
        run_task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.4)
        await runner.cancel()
        await runner.cancel()
        result = await run_task

        assert result.error_code is CliRunErrorCode.CANCELLED
        assert runner._job is None
        assert runner._running is False

    async def test_cancel_race_with_terminal_reap(self, make_spec) -> None:
        # cancel 与终态回收竞争不得抛错或泄漏进程。
        runner = CliAgentRunner(make_spec(["ok"]), timeout_seconds=10)
        run_task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.1)
        await runner.cancel()
        result = await run_task

        assert result.error_code in (CliRunErrorCode.OK, CliRunErrorCode.CANCELLED)
        assert runner._running is False
