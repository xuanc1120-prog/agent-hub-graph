"""子进程最小环境构造。

策略(fail closed):
  - 自动继承面收窄到"当前平台运行绝对路径可执行文件所必需的
    系统级键"(Windows 的 SystemRoot 等); 用户常规 Agent profile、
    HOME 与 PATH 默认一律不继承;
  - 调用方如需 PATH/HOME/TMP 等, 必须通过 extra 显式提供专用值
    (例如指向受控工作区或经过验证的最小 PATH);
  - extra 中出现白名单之外的键直接抛 EnvPolicyError, 不静默丢弃;
  - 全部键与值受数量与长度限制。
"""

from __future__ import annotations

import os
import sys

#: 环境变量数量上限(含系统必需键)。
MAX_ENV_ENTRIES = 16

#: 单个环境键最大长度。
MAX_ENV_KEY_CHARS = 64

#: 单个环境值最大长度。
MAX_ENV_VALUE_CHARS = 4_096

#: 平台系统必需键: 缺失会导致 Python 子进程初始化失败, 自动补齐。
_SYSTEM_REQUIRED: dict[str, tuple[str, ...]] = {
    "win32": ("SystemRoot",),
}

#: 调用方可显式提供的键; 不在此列的 extra 一律拒绝。
_ALLOWED_EXPLICIT: dict[str, tuple[str, ...]] = {
    "win32": ("PATH", "TEMP", "TMP", "SystemRoot"),
    "posix": ("PATH", "HOME", "TMPDIR", "TMP"),
}


class EnvPolicyError(ValueError):
    """环境白名单或尺寸约束被违反时抛出。"""


def allowed_keys() -> tuple[str, ...]:
    """返回当前平台允许显式提供的环境键集合。"""

    family = "win32" if sys.platform.startswith("win") else "posix"
    return _ALLOWED_EXPLICIT.get(family, ())


def _system_required() -> tuple[str, ...]:
    family = "win32" if sys.platform.startswith("win") else "posix"
    return _SYSTEM_REQUIRED.get(family, ())


def _check_size(key: str, value: str) -> None:
    if len(key) > MAX_ENV_KEY_CHARS:
        raise EnvPolicyError(f"env key too long: {len(key)} chars")
    if len(value) > MAX_ENV_VALUE_CHARS:
        raise EnvPolicyError(f"env value too long for key {key[:16]}")


def build_child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """构造子进程环境字典。

    步骤:
      1. 从 ``os.environ`` 仅复制平台系统必需键(缺失则跳过);
      2. 叠加 ``extra``: 白名单外键名直接抛错; 白名单内键覆盖同名值;
      3. 对全部键值执行数量与长度校验。

    注意: HOME/PATH 等用户 profile 类键不会被自动继承, 必须由
    调用方显式给出受控值。
    """

    env: dict[str, str] = {}
    for key in _system_required():
        value = os.environ.get(key)
        if value is not None:
            _check_size(key, value)
            env[key] = value
    allowed = set(allowed_keys())
    for key, value in (extra or {}).items():
        if key not in allowed:
            raise EnvPolicyError(f"env key not whitelisted: {key}")
        _check_size(key, value)
        env[key] = value
    if len(env) > MAX_ENV_ENTRIES:
        raise EnvPolicyError(f"too many env entries: {len(env)}")
    return env


__all__ = [
    "MAX_ENV_ENTRIES",
    "MAX_ENV_KEY_CHARS",
    "MAX_ENV_VALUE_CHARS",
    "EnvPolicyError",
    "allowed_keys",
    "build_child_env",
]
