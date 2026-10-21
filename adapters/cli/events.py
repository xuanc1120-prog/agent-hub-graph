"""HUB-300 传输层事件与结果契约。

本模块定义 CLI Agent 运行器的稳定错误码、脱敏后的有界事件、
以及带不变量校验的 ``CliRunResult``。要点:

  - 两类模型均为冻结模型, 构造后任何字段赋值立即抛错;
  - CliRunResult.events 为不可变 tuple, 无法 append 扩容;
  - CliEvent 的 payload_json 在校验器内统一完成: 必须是合法 JSON
    对象、UTF-8 字节不超过 MAX_EVENT_BYTES、并做幂等脱敏重写,
    因此绕过 create() 的直构同样安全;
  - payload 属性返回全新解析副本, 深层修改不会穿透存储状态。
传输层不产生 ChangeSet 或 AgentResult; 向上映射属于 HUB-310。
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from adapters.cli.redact import sanitize_value
from protocol.common import FrozenStrictModel

# ---------------------------------------------------------------------------
# 有界常量: 单行、单事件、事件总数、总输出上限
# ---------------------------------------------------------------------------

#: 单行最大字节数(UTF-8 编码后)。超限行只上报长度, 不保留原文。
MAX_LINE_BYTES = 65_536

#: 单个事件序列化后的最大字节数。
MAX_EVENT_BYTES = 4_096

#: 单次运行保留的最大事件数; 超出的只计数丢弃。
MAX_EVENTS = 1_000

#: stdout + stderr 合计原始字节上限; 超过即终止进程树。
MAX_TOTAL_OUTPUT_BYTES = 1_000_000

#: 单个 argv token 上限(与 protocol.ArgvToken 一致的运行期防线)。
MAX_ARGV_TOKEN_BYTES = 4_096

#: argv token 数量上限。
MAX_ARGV_TOKENS = 64

#: prompt 最大长度; 保证替换 {prompt} 后 token 不超上限。
MAX_PROMPT_CHARS = 3_000

#: 展开后整个 argv 的字节总量上限(保守值, 低于各平台命令行限制)。
MAX_TOTAL_ARGV_BYTES = 16_000


class CliRunErrorCode(StrEnum):
    """稳定错误码: 调用方据此分支, 不解析自由文本。"""

    OK = "ok"
    EXIT_NON_ZERO = "exit_non_zero"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    OUTPUT_LIMIT_EXCEEDED = "output_limit_exceeded"
    SPAWN_FAILED = "spawn_failed"
    IDENTITY_DRIFT = "identity_drift"
    ORPHANED_DESCENDANTS = "orphaned_descendants"
    PROCESS_ISOLATION = "process_isolation"


def _canonical_payload_json(payload: dict[str, Any]) -> str:
    """脱敏 + 尺寸有界化后的规范化序列化。"""

    encoded = json.dumps(sanitize_value(payload), ensure_ascii=False, default=str, sort_keys=True)
    if len(encoded.encode("utf-8", "replace")) <= MAX_EVENT_BYTES:
        return encoded
    event_type = payload.get("event_type", "cli.unknown")
    stub = {
        "event_type": "cli.event_oversized",
        "original_type": str(event_type)[:128],
        "limit_bytes": MAX_EVENT_BYTES,
    }
    return json.dumps(stub, ensure_ascii=False, sort_keys=True)


class CliEvent(FrozenStrictModel):
    """单条已脱敏的有界流事件(不可变)。

    无论经由 :meth:`create` 还是直接构造, 校验器都会:
      1. 解析 payload_json 并要求其为 JSON 对象(dict);
      2. 幂等脱敏后重新序列化(键名与值模式双层规则);
      3. 拒绝 UTF-8 字节超过 MAX_EVENT_BYTES 的负载(直构超限直接
         抛 ValidationError, 而非静默扩容)。
    """

    seq: int = Field(ge=1)
    stream: str = Field(pattern=r"^(stdout|stderr)$")
    payload_json: str

    @model_validator(mode="after")
    def _validate_and_canonicalize(self) -> CliEvent:
        try:
            parsed = json.loads(self.payload_json)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("payload_json must be a valid JSON object") from exc
        if not isinstance(parsed, dict):
            raise ValueError("payload_json must decode to a JSON object")
        canonical = _canonical_payload_json(parsed)
        if len(canonical.encode("utf-8", "replace")) > MAX_EVENT_BYTES:
            raise ValueError(f"payload exceeds {MAX_EVENT_BYTES} bytes after canonicalization")
        object.__setattr__(self, "payload_json", canonical)
        return self

    @property
    def payload(self) -> dict[str, Any]:
        """返回负载的全新解析副本(外部可安全修改)。"""

        return dict(json.loads(self.payload_json))

    @classmethod
    def create(cls, *, seq: int, stream: str, payload: dict[str, Any]) -> CliEvent:
        """从 dict 构造事件; 超限自动降级为占位负载而非报错。

        供运行器内部使用: 突发输出中的超大单事件降级为
        cli.event_oversized 占位, 保证管道不因单条数据中断。
        """

        return cls(seq=seq, stream=stream, payload_json=_canonical_payload_json(payload))


class CliRunResult(FrozenStrictModel):
    """受限的运行结果(不可变; events 为 tuple, 无法原地扩容)。

    不变量:
      - timed_out / cancelled / exited 至多一个为真;
      - error_code 与布尔标志严格绑定;
      - OK 要求 exited 且 exit_code == 0;
      - EXIT_NON_ZERO 要求 exited 且 exit_code 非零非空;
      - ORPHANED_DESCENDANTS 保留父进程的任意退出码;
      - OUTPUT_LIMIT_EXCEEDED 要求 exited 且 exit_code 非空;
      - SPAWN_FAILED / IDENTITY_DRIFT / PROCESS_ISOLATION 禁止终止标志与 exit_code。
    """

    error_code: CliRunErrorCode
    exit_code: int | None = Field(default=None)
    timed_out: bool = False
    cancelled: bool = False
    exited: bool = False
    duration_seconds: float = Field(default=0.0, ge=0)
    events: tuple[CliEvent, ...] = Field(default_factory=tuple, max_length=MAX_EVENTS)
    dropped_event_count: int = Field(default=0, ge=0)
    truncated_output_bytes: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check_invariants(self) -> CliRunResult:
        flags_true = sum(1 for f in (self.timed_out, self.cancelled, self.exited) if f)
        if flags_true > 1:
            raise ValueError("timed_out / cancelled / exited are mutually exclusive")

        code = self.error_code
        if code is CliRunErrorCode.OK:
            if not (self.exited and self.exit_code == 0):
                raise ValueError("OK requires exited with zero exit code")
        elif code is CliRunErrorCode.EXIT_NON_ZERO:
            if not (self.exited and self.exit_code not in (None, 0)):
                raise ValueError("EXIT_NON_ZERO requires non-zero exit_code")
        elif code is CliRunErrorCode.TIMED_OUT:
            if not self.timed_out or self.exited or self.cancelled:
                raise ValueError("TIMED_OUT binds to timed_out only")
        elif code is CliRunErrorCode.CANCELLED:
            if not self.cancelled or self.exited or self.timed_out:
                raise ValueError("CANCELLED binds to cancelled only")
        elif code is CliRunErrorCode.ORPHANED_DESCENDANTS:
            if not (self.exited and self.exit_code is not None):
                raise ValueError("ORPHANED_DESCENDANTS requires the parent exit code")
        elif code is CliRunErrorCode.OUTPUT_LIMIT_EXCEEDED:
            if not (self.exited and self.exit_code is not None):
                raise ValueError("OUTPUT_LIMIT_EXCEEDED requires exited result")
        else:  # SPAWN_FAILED / IDENTITY_DRIFT / PROCESS_ISOLATION
            if flags_true or self.exit_code is not None:
                raise ValueError(f"{code.value} carries no termination state")
        return self


__all__ = [
    "MAX_ARGV_TOKENS",
    "MAX_ARGV_TOKEN_BYTES",
    "MAX_EVENTS",
    "MAX_EVENT_BYTES",
    "MAX_LINE_BYTES",
    "MAX_PROMPT_CHARS",
    "MAX_TOTAL_ARGV_BYTES",
    "MAX_TOTAL_OUTPUT_BYTES",
    "CliEvent",
    "CliRunErrorCode",
    "CliRunResult",
]
