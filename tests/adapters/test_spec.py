"""CliAgentSpec: 严格校验,占位符与 binary 身份漂移测试。"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from adapters.cli.execution_seal import ExecutionSeal
from adapters.cli.spec import ALLOWED_PLACEHOLDERS, CliAgentSpec, CliSpecError


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expect_reject(match: str):
    # 构造前的路径规范化直接抛 CliSpecError; 模型校验器中的同类
    # 拒绝会由 Pydantic 包装为 ValidationError。两者都属于 fail-closed。
    if not match:
        return pytest.raises((ValidationError, CliSpecError))
    return pytest.raises((ValidationError, CliSpecError), match=match)


@pytest.fixture
def real_executable() -> str:
    return os.path.normpath(sys.executable)


class TestStrictConstruction:
    def test_register_ok(self, real_executable: str) -> None:
        spec = CliAgentSpec.register(executable=real_executable, argv=["-c", "pass"])
        assert spec.expected_sha256 == _sha(Path(real_executable))
        assert spec.version == "1"

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX venv symlink semantics")
    def test_leaf_executable_symlink_is_canonicalized(self, tmp_path: Path) -> None:
        target = Path(sys.executable).resolve(strict=True)
        link = tmp_path / "python"
        link.symlink_to(target)

        spec = CliAgentSpec.register(executable=str(link), argv=["-c", "pass"])

        assert spec.executable == os.path.normpath(str(target))
        assert spec.verify_identity() is True
        link.unlink()
        assert spec.verify_identity() is True

    def test_relative_path_rejected(self) -> None:
        with expect_reject("absolute"):
            CliAgentSpec(
                executable="python.exe",
                argv=[],
                expected_sha256="0" * 64,
            )

    def test_nonexistent_path_rejected(self) -> None:
        with expect_reject("not accessible"):
            CliAgentSpec(
                executable=os.path.join(os.getcwd(), "definitely_missing_9x.py"),
                argv=[],
                expected_sha256="0" * 64,
            )

    def test_directory_rejected(self) -> None:
        with pytest.raises((ValidationError, CliSpecError), match=r"accessible|regular file"):
            CliAgentSpec.register(executable=os.getcwd(), argv=[])

    def test_hash_mismatch_rejected(self, tmp_path: Path) -> None:
        script = tmp_path / "tool.py"
        script.write_text("print('hi')\n", encoding="utf-8")
        with expect_reject("sha256"):
            CliAgentSpec(
                executable=str(script),
                argv=[],
                expected_sha256="0" * 64,
            )

    def test_unnormalized_path_rejected(self) -> None:
        base = os.path.normpath(sys.executable)
        dotted = os.path.join(os.path.dirname(base), ".", os.path.basename(base))
        with pytest.raises((ValidationError, CliSpecError), match="normalized"):
            CliAgentSpec.register(executable=dotted, argv=[])

    def test_external_identity_rejected(self, real_executable: str) -> None:
        with expect_reject("identity is managed"):
            CliAgentSpec(
                executable=real_executable,
                argv=[],
                expected_sha256=_sha(Path(real_executable)),
                identity=object(),
            )


class TestArgvValidation:
    def test_empty_token_rejected(self, real_executable: str) -> None:
        with expect_reject("is empty"):
            CliAgentSpec.register(executable=real_executable, argv=[""])

    def test_nul_byte_rejected(self, real_executable: str) -> None:
        with expect_reject("NUL"):
            CliAgentSpec.register(executable=real_executable, argv=["a\x00b"])

    @pytest.mark.parametrize("bad", ["{task}", "${HOME}", "<file>", "{prompt}{x}"])
    def test_undeclared_placeholder_rejected(self, real_executable: str, bad: str) -> None:
        with expect_reject("undeclared placeholder"):
            CliAgentSpec.register(executable=real_executable, argv=[bad])

    def test_declared_prompt_placeholder_allowed(self, real_executable: str) -> None:
        spec = CliAgentSpec.register(
            executable=real_executable,
            argv=["--goal", "{prompt}"],
            prompt="fix the bug",
        )
        assert "{prompt}" in spec.argv[1]

    def test_nul_prompt_rejected(self, real_executable: str) -> None:
        with expect_reject("NUL"):
            CliAgentSpec.register(
                executable=real_executable,
                argv=["{prompt}"],
                prompt="bad\x00prompt",
            )

    def test_expanded_argv_revalidates_bytes(self, real_executable: str) -> None:
        # 1500 个 CJK 字符 prompt 展开后约 4500 字节, 超过单 token 上限。
        from adapters.cli.spec import CliSpecError

        spec = CliAgentSpec.register(
            executable=real_executable,
            argv=["{prompt}"],
            prompt="\u6d4b" * 1500,
        )
        with pytest.raises(CliSpecError, match="exceeds"):
            spec.expanded_argv()

    def test_expanded_argv_total_budget(self, real_executable: str) -> None:
        from adapters.cli.spec import CliSpecError

        filler = "a" * 3000
        spec = CliAgentSpec.register(
            executable=real_executable,
            argv=[filler, filler, filler, filler, filler, filler],
        )
        with pytest.raises(CliSpecError, match="budget"):
            spec.expanded_argv()

    def test_expanded_argv_happy_path(self, real_executable: str) -> None:
        spec = CliAgentSpec.register(
            executable=real_executable,
            argv=["run", "--goal", "{prompt}"],
            prompt="ship it",
        )
        assert spec.expanded_argv() == ["run", "--goal", "ship it"]

    def test_allowed_set_contains_only_prompt(self) -> None:
        assert {"{prompt}"} == ALLOWED_PLACEHOLDERS

    def test_too_many_tokens_rejected(self, real_executable: str) -> None:
        from adapters.cli.events import MAX_ARGV_TOKENS

        with expect_reject(""):
            CliAgentSpec.register(
                executable=real_executable,
                argv=["t"] * (MAX_ARGV_TOKENS + 1),
            )

    def test_oversized_token_rejected(self, real_executable: str) -> None:
        from adapters.cli.events import MAX_ARGV_TOKEN_BYTES

        with expect_reject("bytes"):
            CliAgentSpec.register(
                executable=real_executable,
                argv=["x" * (MAX_ARGV_TOKEN_BYTES + 1)],
            )


class TestIdentityDrift:
    @pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows share-lock semantics")
    def test_windows_execution_seal_blocks_executable_replacement(self, tmp_path: Path) -> None:
        executable = tmp_path / "python-copy.exe"
        replacement = tmp_path / "replacement.exe"
        shutil.copy2(sys.executable, executable)
        shutil.copy2(sys.executable, replacement)
        spec = CliAgentSpec.register(executable=str(executable), argv=[])

        seal = ExecutionSeal.acquire(spec)
        try:
            with pytest.raises(OSError):
                os.replace(replacement, executable)
            assert seal.path_still_bound() is True
        finally:
            seal.close()

        os.replace(replacement, executable)

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX fd-bound execution")
    def test_posix_execution_seal_runs_verified_object_after_path_swap(
        self, tmp_path: Path
    ) -> None:
        executable = tmp_path / "agent"
        replacement = tmp_path / "replacement"
        executable.write_text(f"#!{sys.executable}\nprint('verified-object')\n", encoding="utf-8")
        replacement.write_text(
            f"#!{sys.executable}\nprint('replacement-object')\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        replacement.chmod(0o700)
        spec = CliAgentSpec.register(executable=str(executable), argv=[])

        with ExecutionSeal.acquire(spec) as seal:
            os.replace(replacement, executable)
            assert seal.path_still_bound() is False
            completed = subprocess.run(
                [seal.spawn_executable],
                pass_fds=seal.pass_fds,
                capture_output=True,
                text=True,
                check=True,
            )

        assert completed.stdout.strip() == "verified-object"

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="Linux memfd sealing")
    def test_posix_execution_seal_freezes_bytes_on_inplace_rewrite(self, tmp_path: Path) -> None:
        executable = tmp_path / "agent"
        executable.write_text(f"#!{sys.executable}\nprint('verified-object')\n", encoding="utf-8")
        executable.chmod(0o700)
        spec = CliAgentSpec.register(executable=str(executable), argv=[])
        original_identity = executable.stat()

        with ExecutionSeal.acquire(spec) as seal:
            executable.write_text(
                f"#!{sys.executable}\nprint('replacement-object')\n",
                encoding="utf-8",
            )
            rewritten_identity = executable.stat()
            assert (rewritten_identity.st_dev, rewritten_identity.st_ino) == (
                original_identity.st_dev,
                original_identity.st_ino,
            )
            assert seal.path_still_bound() is False
            completed = subprocess.run(
                [seal.spawn_executable],
                pass_fds=seal.pass_fds,
                capture_output=True,
                text=True,
                check=True,
            )

        assert completed.stdout.strip() == "verified-object"

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX executable swap")
    async def test_runner_detects_swap_at_seal_spawn_barrier(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from adapters.cli.events import CliRunErrorCode
        from adapters.cli.runner import CliAgentRunner

        marker = tmp_path / "replacement-ran.txt"
        executable = tmp_path / "agent"
        replacement = tmp_path / "replacement"
        executable.write_text(f"#!{sys.executable}\nprint('verified')\n", encoding="utf-8")
        replacement.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('bad')\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        replacement.chmod(0o700)
        spec = CliAgentSpec.register(executable=str(executable), argv=[])
        original_acquire = ExecutionSeal.acquire.__func__

        def acquire_then_swap(cls, target: CliAgentSpec) -> ExecutionSeal:
            seal = original_acquire(cls, target)
            os.replace(replacement, executable)
            return seal

        monkeypatch.setattr(ExecutionSeal, "acquire", classmethod(acquire_then_swap))
        result = await CliAgentRunner(spec, timeout_seconds=5).run()

        assert result.error_code is CliRunErrorCode.IDENTITY_DRIFT
        assert marker.exists() is False

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX in-place rewrite")
    async def test_runner_rejects_inplace_rewrite_at_seal_spawn_barrier(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from adapters.cli.events import CliRunErrorCode
        from adapters.cli.runner import CliAgentRunner

        marker = tmp_path / "replacement-ran.txt"
        executable = tmp_path / "agent"
        executable.write_text(f"#!{sys.executable}\nprint('verified')\n", encoding="utf-8")
        executable.chmod(0o700)
        spec = CliAgentSpec.register(executable=str(executable), argv=[])
        original_inode = executable.stat().st_ino
        original_acquire = ExecutionSeal.acquire.__func__

        def acquire_then_rewrite(cls, target: CliAgentSpec) -> ExecutionSeal:
            seal = original_acquire(cls, target)
            executable.write_text(
                f"#!{sys.executable}\nfrom pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('bad')\n",
                encoding="utf-8",
            )
            assert executable.stat().st_ino == original_inode
            return seal

        monkeypatch.setattr(ExecutionSeal, "acquire", classmethod(acquire_then_rewrite))
        result = await CliAgentRunner(spec, timeout_seconds=5).run()

        assert result.error_code is CliRunErrorCode.IDENTITY_DRIFT
        assert marker.exists() is False

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="Linux memfd sealing")
    async def test_runner_executes_sealed_bytes_when_rewritten_inside_spawn(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from adapters.cli.events import CliRunErrorCode
        from adapters.cli.runner import CliAgentRunner

        marker = tmp_path / "replacement-ran.txt"
        executable = tmp_path / "agent"
        executable.write_text(
            f'#!{sys.executable}\nprint(\'{{"event_type": "verified.object"}}\')\n',
            encoding="utf-8",
        )
        executable.chmod(0o700)
        spec = CliAgentSpec.register(executable=str(executable), argv=[])
        original_inode = executable.stat().st_ino
        runner = CliAgentRunner(spec, timeout_seconds=5)
        original_spawn = runner._spawn

        async def rewrite_then_spawn(argv: list[str], env: dict[str, str]):
            executable.write_text(
                f"#!{sys.executable}\nfrom pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('bad')\n",
                encoding="utf-8",
            )
            assert executable.stat().st_ino == original_inode
            return await original_spawn(argv, env)

        monkeypatch.setattr(runner, "_spawn", rewrite_then_spawn)
        result = await runner.run()

        assert result.error_code is CliRunErrorCode.OK
        assert marker.exists() is False
        assert any(event.payload.get("event_type") == "verified.object" for event in result.events)

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux memfd sealing")
    async def test_memfd_tamper_before_add_seals_fails_closed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import fcntl

        import adapters.cli.execution_seal as seal_mod
        from adapters.cli.events import CliRunErrorCode
        from adapters.cli.runner import CliAgentRunner

        marker = tmp_path / "tampered-memfd-ran.txt"
        executable = tmp_path / "agent"
        executable.write_text(
            f'#!{sys.executable}\nprint(\'{{"event_type": "verified.object"}}\')\n',
            encoding="utf-8",
        )
        executable.chmod(0o700)
        spec = CliAgentSpec.register(executable=str(executable), argv=[])
        malicious = (
            f"#!{sys.executable}\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('bad')\n"
        ).encode()
        real_fcntl = fcntl.fcntl
        real_create = seal_mod._create_linux_memfd
        tampered = False
        attacker_fd: int | None = None

        def create_with_preopened_writer() -> int:
            nonlocal attacker_fd
            fd = real_create()
            attacker_fd = os.open(f"/proc/{os.getpid()}/fd/{fd}", os.O_RDWR)
            return fd

        def tamper_before_sealing(fd: int, command: int, argument: int = 0):
            nonlocal attacker_fd, tampered
            if command == seal_mod._F_ADD_SEALS and not tampered:
                assert attacker_fd is not None
                try:
                    os.ftruncate(attacker_fd, 0)
                    os.write(attacker_fd, malicious)
                finally:
                    os.close(attacker_fd)
                    attacker_fd = None
                tampered = True
            return real_fcntl(fd, command, argument)

        monkeypatch.setattr(seal_mod, "_create_linux_memfd", create_with_preopened_writer)
        monkeypatch.setattr(fcntl, "fcntl", tamper_before_sealing)
        try:
            result = await CliAgentRunner(spec, timeout_seconds=5).run()
        finally:
            if attacker_fd is not None:
                os.close(attacker_fd)

        assert tampered is True
        assert result.error_code is CliRunErrorCode.IDENTITY_DRIFT
        assert marker.exists() is False

    def test_content_drift_detected(self, tmp_path: Path) -> None:
        script = tmp_path / "agent.py"
        script.write_text("v1\n", encoding="utf-8")
        spec = CliAgentSpec.register(executable=str(script), argv=[])
        script.write_text("v2 replaced by attacker\n", encoding="utf-8")
        assert spec.verify_identity() is False

    def test_same_bytes_replacement_detected(self, tmp_path: Path) -> None:
        # 相同字节替换: hash 不变,但 dev/ino 文件身份变化必须被识别。
        script = tmp_path / "agent.py"
        script.write_text("stable-bytes\n", encoding="utf-8")
        spec = CliAgentSpec.register(executable=str(script), argv=[])
        replaced = tmp_path / "rebuild.py"
        replaced.write_text("stable-bytes\n", encoding="utf-8")
        os.replace(replaced, script)
        assert spec.verify_identity() is False

    def test_identity_snapshot_is_frozen(self, tmp_path: Path) -> None:
        # 复审回归: 篡改 identity 快照必须失败, 无法绕过漂移检测。
        import dataclasses

        from adapters.cli.spec import FileIdentity

        script = tmp_path / "agent.py"
        script.write_text("stable-bytes\n", encoding="utf-8")
        spec = CliAgentSpec.register(executable=str(script), argv=[])
        replaced = tmp_path / "rebuild.py"
        replaced.write_text("stable-bytes\n", encoding="utf-8")
        os.replace(replaced, script)
        assert spec.verify_identity() is False
        bound = spec.identity
        assert isinstance(bound, FileIdentity)
        with pytest.raises(dataclasses.FrozenInstanceError):
            bound.dev = 0  # type: ignore[misc]

    def test_spec_model_is_frozen(self, real_executable: str) -> None:
        from pydantic import ValidationError

        spec = CliAgentSpec.register(executable=real_executable, argv=["a"])
        with pytest.raises(ValidationError):
            spec.prompt = "tampered"
        with pytest.raises(ValidationError):
            spec.executable = str(Path(real_executable).parent)

    def test_argv_tuple_is_immutable(self, real_executable: str) -> None:
        spec = CliAgentSpec.register(executable=real_executable, argv=["a"])
        assert isinstance(spec.argv, tuple)
        with pytest.raises(TypeError):
            spec.argv[0] = ""  # type: ignore[index]

    def test_prompt_braces_are_literal_data(self, real_executable: str) -> None:
        # prompt 是数据而非结构: 大括号/$ 形态按字面量进入单个 token,
        # 无 shell 参与, 不做占位符语义判定。
        spec = CliAgentSpec.register(
            executable=real_executable,
            argv=["run", "{prompt}"],
            prompt='hello "${HOME}" and {file}',
        )
        assert spec.expanded_argv()[1] == 'hello "${HOME}" and {file}'

    @pytest.mark.skipif(not sys.platform.startswith("win"), reason="junction 为 Windows 特性")
    def test_junction_ancestor_rejected_windows(self, tmp_path: Path) -> None:
        import _winapi

        real_dir = tmp_path / "real"
        real_dir.mkdir()
        script = real_dir / "tool.py"
        script.write_text("ok\n", encoding="utf-8")
        link = tmp_path / "link"
        _winapi.CreateJunction(str(real_dir), str(link))
        junctioned = link / "tool.py"
        with expect_reject("symlink|junction|reparse|component"):
            CliAgentSpec.register(executable=os.path.normpath(str(junctioned)), argv=[])

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlink 语义")
    def test_symlinked_ancestor_rejected_posix(self, tmp_path: Path) -> None:
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        script = real_dir / "tool.py"
        script.write_text("ok\n", encoding="utf-8")
        link = tmp_path / "link"
        os.symlink(real_dir, link)
        with expect_reject("symlink|junction|reparse|component"):
            CliAgentSpec.register(executable=os.path.normpath(str(link / "tool.py")), argv=[])

    def test_deleted_file_detected(self, tmp_path: Path) -> None:
        script = tmp_path / "gone.py"
        script.write_text("x\n", encoding="utf-8")
        spec = CliAgentSpec.register(executable=str(script), argv=[])
        script.unlink()
        assert spec.verify_identity() is False

    def test_ancestor_swap_same_file_detected(self, tmp_path: Path) -> None:
        # P1-1 回归: 祖先目录被替换为新目录, 但叶子为同一原文件
        # (同 inode、同内容)时, 运行前复核也必须返回 False。
        parent = tmp_path / "parent"
        real = parent / "real"
        real.mkdir(parents=True)
        script = real / "tool.py"
        script.write_text("same-bytes\n", encoding="utf-8")
        spec = CliAgentSpec.register(executable=str(script), argv=[])
        assert spec.verify_identity() is True
        backup = parent / "real_orig"
        os.rename(real, backup)
        real.mkdir()
        # 同一原文件搬回新目录: 叶子身份与内容均不变, 祖先已漂移。
        os.replace(backup / "tool.py", real / "tool.py")
        assert spec.verify_identity() is False

    @pytest.mark.skipif(not sys.platform.startswith("win"), reason="junction 为 Windows 特性")
    def test_junction_ancestor_after_register_rejected_windows(self, tmp_path: Path) -> None:
        # P1-1 回归: 注册后把父目录换成指向原目录的 junction,
        # verify_identity() 必须重新校验祖先链并返回 False。
        import _winapi

        real = tmp_path / "real"
        real.mkdir()
        script = real / "tool.py"
        script.write_text("ok\n", encoding="utf-8")
        spec = CliAgentSpec.register(executable=str(script), argv=[])
        assert spec.verify_identity() is True
        backup = tmp_path / "real_orig"
        os.rename(real, backup)
        _winapi.CreateJunction(str(backup), str(real))
        assert spec.verify_identity() is False

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlink 语义")
    def test_symlink_ancestor_after_register_rejected_posix(self, tmp_path: Path) -> None:
        # P1-1 回归: 注册后把父目录换成指向原目录的 symlink,
        # verify_identity() 必须重新校验祖先链并返回 False。
        real = tmp_path / "real"
        real.mkdir()
        script = real / "tool.py"
        script.write_text("ok\n", encoding="utf-8")
        spec = CliAgentSpec.register(executable=str(script), argv=[])
        assert spec.verify_identity() is True
        backup = tmp_path / "real_orig"
        os.rename(real, backup)
        os.symlink(backup, real)
        assert spec.verify_identity() is False

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink 需要特权")
    def test_symlink_swap_detected(self, tmp_path: Path) -> None:
        real = tmp_path / "real.py"
        real.write_text("original\n", encoding="utf-8")
        spec = CliAgentSpec.register(executable=str(real), argv=[])
        evil = tmp_path / "evil.py"
        evil.write_text("attacker\n", encoding="utf-8")
        link = tmp_path / "link.py"
        os.symlink(evil, link)
        os.replace(link, real)
        assert spec.verify_identity() is False

    async def test_runner_refuses_drifted_binary_before_spawn(self, tmp_path: Path) -> None:
        from adapters.cli.events import CliRunErrorCode
        from adapters.cli.runner import CliAgentRunner

        script = tmp_path / "drift.py"
        script.write_text("print('ok')\n", encoding="utf-8")
        spec = CliAgentSpec.register(executable=str(script), argv=[])
        script.write_bytes(b"tampered")
        runner = CliAgentRunner(spec, timeout_seconds=5)
        result = await runner.run()
        assert result.error_code is CliRunErrorCode.IDENTITY_DRIFT


class TestFileIdentity:
    def test_regular_file_flag(self, tmp_path: Path) -> None:
        script = tmp_path / "f.py"
        script.write_text("1\n", encoding="utf-8")
        mode = script.stat().st_mode
        assert stat.S_ISREG(mode)
