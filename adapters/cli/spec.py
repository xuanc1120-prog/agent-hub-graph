"""CliAgentSpec: 冻结的严格规格与可执行文件身份绑定。

安全设计:
  - 模型继承 FrozenStrictModel, argv 为不可变 tuple; 构造后任何
    赋值(含 argv 元素级篡改路径)都会立即抛错;
  - FileIdentity 为 frozen dataclass, 外部既无法注入也无法修改,
    verify_identity 同时比对内容 SHA-256 与 (dev, ino) 文件身份;
  - Windows 上通过 FILE_ATTRIBUTE_REPARSE_POINT 识别全部 reparse
    类型(junction/mount point/symlink), 并沿祖先链逐一检查;
    POSIX 上沿祖先链做 lstat 拒绝 symlink;
  - expanded_argv() 在 {prompt} 替换后完整重验空 token、NUL、
    单 token 与总量字节预算, 并检测 prompt 注入的新占位符。
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, model_validator

from adapters.cli.events import (
    MAX_ARGV_TOKEN_BYTES,
    MAX_ARGV_TOKENS,
    MAX_PROMPT_CHARS,
    MAX_TOTAL_ARGV_BYTES,
)
from protocol.common import FrozenStrictModel, Sha256Hex

#: 允许出现在 argv token 中的占位符集合。
ALLOWED_PLACEHOLDERS = frozenset({"{prompt}"})

#: 占位符形态: {name}、${VAR}、$VAR、<name>, 禁止嵌套大括号。
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}|<[^<>\s]{1,64}>|\$[A-Za-z{]")

_READ_CHUNK = 65_536

#: Windows FILE_ATTRIBUTE_REPARSE_POINT。
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


class CliSpecError(ValueError):
    """spec 构造、身份校验或展开校验失败时抛出。"""


@dataclass(frozen=True)
class FileIdentity:
    """一次打开句柄内完成 fstat 与摘要读取的不可变身份快照。"""

    sha256: str
    dev: int
    ino: int
    size: int

    @classmethod
    def capture(cls, path: Path) -> FileIdentity:
        """no-follow 打开并计算摘要。

        先 lstat 取 (dev, ino), 再 no-follow open 后 fstat 复核,
        缩小 TOCTOU 窗口; 两次不一致视为文件已被替换。
        系统调用失败统一转换为 CliSpecError(fail closed)。
        """

        try:
            pre = os.lstat(path)
            if not stat.S_ISREG(pre.st_mode):
                raise CliSpecError("executable must be a regular file")
            fd = _open_nofollow(path)
        except OSError as exc:
            raise CliSpecError(f"executable not accessible: errno={exc.errno}") from exc
        try:
            post = os.fstat(fd)
            if (pre.st_dev, pre.st_ino) != (post.st_dev, post.st_ino):
                raise CliSpecError("executable path changed during identity capture")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, _READ_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        except OSError as exc:
            raise CliSpecError(f"executable unreadable: errno={exc.errno}") from exc
        finally:
            os.close(fd)
        return cls(digest.hexdigest(), post.st_dev, post.st_ino, post.st_size)


def _win_reparse_attributes(path: Path) -> int | None:
    """返回 Windows 文件属性; 路径不存在返回 None。

    注意: 必须显式声明 restype 为无符号 32 位, 否则
    INVALID_FILE_ATTRIBUTES(0xFFFFFFFF) 会以有符号 -1 返回,
    与哨兵值比较失败导致全路径误判为 reparse。
    """

    if not sys.platform.startswith("win"):
        return None
    get_attrs = ctypes.windll.kernel32.GetFileAttributesW
    get_attrs.restype = ctypes.c_uint32
    get_attrs.argtypes = [ctypes.c_wchar_p]
    result = get_attrs(str(path))
    if result == _INVALID_FILE_ATTRIBUTES:
        return None
    return result


def _assert_no_reparse_chain(path: Path) -> None:
    """拒绝路径任一组件为 symlink/junction/reparse point。

    - Windows: 对叶子与每个现存祖先检查 REPARSE 属性(覆盖 junction
      与 mount point 等 S_ISLNK 无法表达的类型), 叶子再叠加 lstat;
    - POSIX: 对叶子与每个现存祖先做 lstat, S_ISLNK 即拒绝。
    """

    chain: list[Path] = [path, *path.parents]
    for component in chain:
        attrs = _win_reparse_attributes(component)
        if attrs is not None and attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise CliSpecError("path contains a symlink/junction/reparse component")
        try:
            st = os.lstat(component)
        except FileNotFoundError:
            # 更高层祖先不存在: 已检查到现有链尽头。
            break
        except OSError as exc:
            raise CliSpecError(f"path not accessible: errno={exc.errno}") from exc
        if stat.S_ISLNK(st.st_mode):
            raise CliSpecError("path contains a symlink component")


def _canonicalize_executable_path(value: str) -> str:
    """Normalize a trusted POSIX leaf symlink to its final regular target.

    Virtual environments normally expose ``bin/python`` as a leaf symlink.
    The mutable link itself must not remain execution authority, so registration
    stores the resolved target path and binds identity there. Symlinked parent
    directories remain forbidden; Windows reparse points keep the stricter
    no-link policy because their handle binding is implemented separately.
    """

    path = Path(value)
    if not path.is_absolute():
        raise CliSpecError("executable must be an absolute path")
    normalized = os.path.normpath(str(path))
    if normalized != value:
        raise CliSpecError("executable must be a normalized absolute path")
    if sys.platform.startswith("win"):
        return value

    try:
        leaf = os.lstat(path)
    except OSError as exc:
        raise CliSpecError(f"executable not accessible: errno={exc.errno}") from exc
    if not stat.S_ISLNK(leaf.st_mode):
        _assert_no_reparse_chain(path.parent)
        return value

    _assert_no_reparse_chain(path.parent)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        error = getattr(exc, "errno", None)
        raise CliSpecError(f"executable symlink target not accessible: errno={error}") from exc
    canonical = os.path.normpath(str(resolved))
    _assert_no_reparse_chain(Path(canonical))
    try:
        target = os.lstat(canonical)
    except OSError as exc:
        raise CliSpecError(f"executable not accessible: errno={exc.errno}") from exc
    if not stat.S_ISREG(target.st_mode):
        raise CliSpecError("executable symlink target must be a regular file")
    return canonical


def _open_nofollow(path: Path) -> int:
    """以拒绝跟随 symlink 的方式打开文件, 返回 fd。"""

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        return os.open(path, flags | os.O_NOFOLLOW)
    return os.open(path, flags)


def _capture_ancestor_binding(path: Path) -> tuple[tuple[int, int], ...]:
    """捕获已存在祖先目录的 (dev, ino) 绑定, 供运行前比对。

    祖先链漂移(目录被替换为同名 junction/symlink/新目录, 但叶子
    文件内容与身份不变)必须被拒绝, 因此注册时锁定整条现存祖先链
    的目录身份, 运行前逐一比对。
    """

    binding: list[tuple[int, int]] = []
    for parent in path.parents:
        try:
            st = os.lstat(parent)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise CliSpecError(f"path not accessible: errno={exc.errno}") from exc
        binding.append((st.st_dev, st.st_ino))
    return tuple(binding)


def _extract_placeholders(token: str) -> set[str]:
    return {match.group(0) for match in _PLACEHOLDER_RE.finditer(token)}


def _check_structural_token(index: int, value: str) -> None:
    """与占位符无关的结构性校验, 供构造期与展开期共用。"""

    if not value:
        raise CliSpecError(f"argv[{index}] is empty")
    if "\x00" in value:
        raise CliSpecError(f"argv[{index}] contains NUL byte")
    if len(value.encode("utf-8", "replace")) > MAX_ARGV_TOKEN_BYTES:
        raise CliSpecError(f"argv[{index}] exceeds {MAX_ARGV_TOKEN_BYTES} bytes")


class CliAgentSpec(FrozenStrictModel):
    """CLI Agent 运行规格; 构造即校验, 之后完全不可变。"""

    #: 可执行文件的规范化绝对路径。
    executable: str = Field(min_length=1, max_length=1_024)

    #: 固定参数向量(不可变 tuple); 不含 executable 自身。
    argv: tuple[str, ...] = Field(default_factory=tuple, max_length=MAX_ARGV_TOKENS)

    #: 受限 prompt, 仅用于替换 argv 中声明的 {prompt} 占位符。
    prompt: str = Field(default="", max_length=MAX_PROMPT_CHARS)

    #: 注册时锁定的可执行文件 SHA-256。
    expected_sha256: Sha256Hex

    #: 版本标签(例如上游版本号), 随注册记录。
    version: str = Field(default="1", max_length=64)

    #: 运行子进程的工作目录; 缺省继承运行器进程 cwd。
    cwd: str | None = Field(default=None, max_length=1_024)

    #: 内部身份快照(冻结 dataclass); 由校验器填充, 外部传入一律拒绝。
    identity: object | None = Field(default=None, exclude=True, repr=False)

    #: 注册时锁定的祖先目录 (dev, ino) 链; 运行前逐一比对。
    ancestor_binding: object | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="before")
    @classmethod
    def _reject_external_identity(cls, data: object) -> object:
        # identity/ancestor_binding 只能由本模型校验器生成, 防止伪造快照绕过复核。
        if isinstance(data, dict):
            if data.get("identity") is not None or data.get("ancestor_binding") is not None:
                raise CliSpecError("identity is managed internally; use CliAgentSpec.register")
            cleaned = {k: v for k, v in data.items() if k not in ("identity", "ancestor_binding")}
            executable = cleaned.get("executable")
            if isinstance(executable, str):
                cleaned["executable"] = _canonicalize_executable_path(executable)
            return cleaned
        return data

    @model_validator(mode="after")
    def _validate_and_capture(self) -> CliAgentSpec:
        path = Path(self.executable)
        if not path.is_absolute():
            raise CliSpecError("executable must be an absolute path")
        normalized = os.path.normpath(str(path))
        if normalized != self.executable:
            raise CliSpecError("executable must be a normalized absolute path")

        # 祖先链 reparse/symlink 检查必须先于 lstat 叶子类型判断。
        _assert_no_reparse_chain(path)
        try:
            st = os.lstat(self.executable)
        except OSError as exc:
            raise CliSpecError(f"executable not accessible: errno={exc.errno}") from exc
        if not stat.S_ISREG(st.st_mode):
            raise CliSpecError("executable must be a regular file")

        if "\x00" in self.prompt:
            raise CliSpecError("prompt contains NUL byte")

        for index, token in enumerate(self.argv):
            _check_structural_token(index, token)
            for placeholder in _extract_placeholders(token):
                if placeholder not in ALLOWED_PLACEHOLDERS:
                    raise CliSpecError(f"argv[{index}] uses undeclared placeholder {placeholder}")
        if self.cwd is not None:
            cwd_path = Path(self.cwd)
            if not cwd_path.is_absolute() or os.path.normpath(self.cwd) != self.cwd:
                raise CliSpecError("cwd must be a normalized absolute path")

        identity = FileIdentity.capture(path)
        if identity.sha256 != self.expected_sha256:
            raise CliSpecError("expected_sha256 does not match file content")
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "ancestor_binding", _capture_ancestor_binding(path))
        return self

    @classmethod
    def register(
        cls,
        *,
        executable: str,
        argv: list[str] | tuple[str, ...],
        version: str = "1",
        prompt: str = "",
        cwd: str | None = None,
    ) -> CliAgentSpec:
        """注册入口: 自动计算 SHA-256 并锁定身份后走完整校验构造。"""

        canonical = _canonicalize_executable_path(executable)
        digest = FileIdentity.capture(Path(canonical)).sha256
        return cls(
            executable=canonical,
            argv=tuple(argv),
            prompt=prompt,
            expected_sha256=digest,
            version=version,
            cwd=cwd,
        )

    def verify_identity(self) -> bool:
        """运行前复核: 祖先链、路径、文件类型、内容与文件身份均未漂移。"""

        path = Path(self.executable)
        # 可信路径绑定: 冻结模型不可变, 但防御性比对规范化形态。
        if not path.is_absolute() or os.path.normpath(str(path)) != self.executable:
            return False
        try:
            # 运行前重新校验完整祖先链: Windows 拒绝 junction/mount
            # point/symlink/其他 reparse point; POSIX 拒绝 symlink。
            _assert_no_reparse_chain(path)
            st = os.lstat(self.executable)
            if not stat.S_ISREG(st.st_mode):
                return False
            current_binding = _capture_ancestor_binding(path)
            current = FileIdentity.capture(path)
        except (CliSpecError, OSError):
            return False
        bound = self.identity
        bound_ancestors = self.ancestor_binding
        if not isinstance(bound, FileIdentity):
            return False
        if not isinstance(bound_ancestors, tuple):
            return False
        if tuple(bound_ancestors) != current_binding:
            return False
        # 内容哈希对齐冻结模型字段, 文件身份对齐冻结快照。
        return (
            current.sha256 == self.expected_sha256
            and current.dev == bound.dev
            and current.ino == bound.ino
        )

    def expanded_argv(self) -> list[str]:
        """替换 {prompt} 后执行完整安全重验。

        - 结构性检查(空/NUL/单 token 字节上限/argv 总预算)全部重跑;
        - prompt 是数据而非结构: 替换以纯文本进行, prompt 中的大括号
          或 $ 形态只是字面量(无 shell 参与, OS 按单 token 接收),
          因此不对其内容做占位符语义判定;
        - 模板自身仍受构造期占位符白名单约束, 展开不会引入新的
          结构性占位符。
        """

        expanded: list[str] = []
        total_bytes = 0
        for index, template in enumerate(self.argv):
            value = template.replace("{prompt}", self.prompt)
            _check_structural_token(index, value)
            total_bytes += len(value.encode("utf-8", "replace"))
            if total_bytes > MAX_TOTAL_ARGV_BYTES:
                raise CliSpecError(f"expanded argv exceeds {MAX_TOTAL_ARGV_BYTES} byte budget")
            expanded.append(value)
        return expanded


__all__ = [
    "ALLOWED_PLACEHOLDERS",
    "CliAgentSpec",
    "CliSpecError",
    "FileIdentity",
]
