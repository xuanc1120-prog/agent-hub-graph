"""通用 CLI Agent 传输层(HUB-300)。

提供与具体 Agent 无关的 :class:`CliAgentSpec` 与
:class:`CliAgentRunner`; 结果为受限的 :class:`CliRunResult`,
不产生 ChangeSet 或 AgentResult。
"""

from adapters.cli.env import EnvPolicyError, build_child_env
from adapters.cli.events import CliEvent, CliRunErrorCode, CliRunResult
from adapters.cli.runner import CliAgentRunner, CliRunnerError, spawn_kwargs
from adapters.cli.spec import CliAgentSpec, CliSpecError

__all__ = [
    "CliAgentRunner",
    "CliAgentSpec",
    "CliEvent",
    "CliRunErrorCode",
    "CliRunResult",
    "CliRunnerError",
    "CliSpecError",
    "EnvPolicyError",
    "build_child_env",
    "spawn_kwargs",
]
