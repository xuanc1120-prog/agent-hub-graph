"""StreamAssembler,脱敏与环境白名单的单元测试(平台无关)。"""

from __future__ import annotations

import json

import pytest

from adapters.cli import env as env_mod
from adapters.cli.events import MAX_LINE_BYTES
from adapters.cli.redact import redact_string, sanitize_value
from adapters.cli.streams import StreamAssembler


class Lines:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.oversized: list[int] = []
        self.decode_errors = 0

    def assembler(self) -> StreamAssembler:
        return StreamAssembler(
            on_line=self.lines.append,
            on_oversized=self.oversized.append,
            on_decode_error=lambda _dropped: setattr(self, "decode_errors", self.decode_errors + 1),
        )


class TestStreamAssembler:
    def test_single_chunk_lines(self) -> None:
        sink = Lines()
        asm = sink.assembler()
        asm.feed(b'{"a": 1}\n{"b": 2}\n')
        asm.flush()
        assert sink.lines == ['{"a": 1}', '{"b": 2}']
        assert not sink.oversized

    def test_fragmented_bytes_across_chunks(self) -> None:
        sink = Lines()
        asm = sink.assembler()
        raw = b'{"k": "v"}\n'
        for i in range(0, len(raw), 3):
            asm.feed(raw[i : i + 3])
        asm.flush()
        assert sink.lines == ['{"k": "v"}']

    def test_multibyte_char_split_mid_sequence(self) -> None:
        # emoji U+1F600 的 UTF-8 为 4 字节,拆开后必须仍还原成一行。
        text = '{"note": "\U0001f600"}'
        raw = (text + "\n").encode("utf-8")
        sink = Lines()
        asm = sink.assembler()
        cut = raw.index("\U0001f600".encode("utf-8")) + 2
        asm.feed(raw[:cut])
        asm.feed(raw[cut:])
        asm.flush()
        assert sink.lines == [text]

    def test_crlf_and_no_trailing_newline(self) -> None:
        sink = Lines()
        asm = sink.assembler()
        asm.feed(b"a\r\nb")
        asm.flush()
        assert sink.lines == ["a\r", "b"]

    def test_invalid_utf8_reports_without_raw(self) -> None:
        sink = Lines()
        asm = sink.assembler()
        asm.feed(b"\xff\xfe ok\n")
        asm.flush()
        # 连续非法字节合并为一次错误; 其后的合法数据继续解析。
        assert sink.decode_errors == 1
        assert sink.lines == [" ok"]
        assert b"\xff" not in "".join(sink.lines).encode("utf-8", "replace")

    def test_oversized_line_dropped_but_stream_continues(self) -> None:
        sink = Lines()
        asm = sink.assembler()
        big = b"x" * (MAX_LINE_BYTES + 10)
        asm.feed(big + b"\nafter=1\n")
        asm.flush()
        assert len(sink.oversized) == 1
        assert sink.oversized[0] >= MAX_LINE_BYTES
        assert sink.lines == ["after=1"]

    def test_oversized_multichunk_line_no_partial_content(self) -> None:
        sink = Lines()
        asm = sink.assembler()
        half = MAX_LINE_BYTES // 2 + 8
        asm.feed(b"x" * half)
        asm.feed(b"x" * half)
        asm.feed(b"\nnext\n")
        asm.flush()
        assert len(sink.oversized) == 1
        assert sink.lines == ["next"]


class TestRedaction:
    @pytest.mark.parametrize(
        "key",
        ["password_hash", "token_value", "api_key_value", "primary_password"],
    )
    def test_sensitive_word_anywhere_in_key_is_redacted(self, key: str) -> None:
        assert sanitize_value({key: "opaque-value"})[key] == "[REDACTED]"

    @pytest.mark.parametrize(
        "key",
        ["primary_key", "foreign_key", "unique_key", "candidate_key"],
    )
    def test_only_exact_database_terms_are_exempt(self, key: str) -> None:
        assert sanitize_value({key: "identifier"})[key] == "identifier"

    def test_sensitive_key_replaces_value_whole(self) -> None:
        # 复审回归: 敏感键名的值必须整体替换, 包括非字符串值。
        cleaned = sanitize_value({"token": "TOP_SECRET_VALUE", "password": 123456, "keep": "x"})
        assert cleaned["token"] == "[REDACTED]"
        assert cleaned["password"] == "[REDACTED]"
        assert cleaned["keep"] == "x"

    def test_sensitive_key_nested_and_listed(self) -> None:
        data = {"nested": [{"secret": {"deep": 1}}], "items": [{"api_key": "k"}]}
        cleaned = sanitize_value(data)
        assert cleaned["nested"][0]["secret"] == "[REDACTED]"
        assert cleaned["items"][0]["api_key"] == "[REDACTED]"

    def test_composite_credential_keys(self) -> None:
        # 复审回归: 常见组合形态(分隔符 + camelCase)必须命中。
        payload = {
            "AWS_SECRET_ACCESS_KEY": "OUTSIDE_SECRET",
            "client_secret": "CLIENT_SECRET",
            "secret_access_key": "s1",
            "secret_key": "s2",
            "refresh_token": "r1",
            "oauth_token": "o1",
            "db_pass": "d1",
            "awsSecretAccessKey": "c1",
        }
        cleaned = sanitize_value(payload)
        for key in payload:
            assert cleaned[key] == "[REDACTED]", key

    def test_non_sensitive_keys_untouched(self) -> None:
        data = {
            "token_count": 42,
            "tokenizer": "t",
            "note": "ok",
            "primary_key": "id_pk",
            "foreign_key": "fk_ref",
        }
        assert sanitize_value(data) == data

    def test_secret_pair_masked(self) -> None:
        out = redact_string("api_key=TOP_SECRET_VALUE")
        assert "TOP_SECRET_VALUE" not in out
        assert "[REDACTED]" in out

    def test_colon_form_and_header(self) -> None:
        assert "s3cr3t" not in redact_string("Authorization: Bearer s3cr3ttoken99")
        assert "hunter2" not in redact_string("password: hunter2")

    def test_long_hex_masked(self) -> None:
        hexblob = "f" * 40
        out = redact_string(f"hash {hexblob} end")
        assert hexblob not in out

    def test_normal_text_untouched(self) -> None:
        text = "build finished in 3.2s, 12 files changed"
        assert redact_string(text) == text

    def test_sanitize_nested_structure(self) -> None:
        data = {
            "msg": "token=abc123",
            "nested": [{"auth": "Bearer xyzsecret99"}],
            "keep": 7,
        }
        cleaned = sanitize_value(data)
        dumped = repr(cleaned)
        assert "abc123" not in dumped
        assert "xyzsecret99" not in dumped
        assert cleaned["keep"] == 7

    def test_cross_chunk_split_secret_is_redacted(self) -> None:
        # 关键回归: secret 跨 chunk 拆分。装配层负责完整还原整行,
        # 脱敏发生在行解析之后,因此拆分不影响命中。
        secret_line = '{"log": "api_key=SPLIT_SECRET_XY"}'
        raw = (secret_line + "\n").encode()
        sink = Lines()
        asm = sink.assembler()
        mid = raw.index(b"SPLIT_SECRET") + 4
        asm.feed(raw[:mid])
        asm.feed(raw[mid:])
        asm.flush()
        assert sink.lines == [secret_line]
        cleaned = sanitize_value(json.loads(sink.lines[0]))
        assert "SPLIT_SECRET_XY" not in repr(cleaned)

    @pytest.mark.parametrize(
        "text",
        [
            "AWS_SECRET_ACCESS_KEY=OUTSIDE_SECRET",
            "DB_PASS=secret123",
            "client_secret=secret123",
            "oauth_token=secret123",
            "secret_access_key=secret123",
            "oauth_token: secret123",
            "export DB_PASS=secret123",
            "export secret_access_key='secret123'",
            'client_secret="secret123"',
            "api-key=abc123",
            "api key=abc123",
            "awsSecretAccessKey=c1",
        ],
    )
    def test_string_kv_composite_secrets_redacted(self, text: str) -> None:
        # P1-4 回归: 字符串中的 key=value / key:value / shell export
        # 必须使用与 dict 键相同的分词与敏感性判定。
        out = redact_string(text)
        assert "OUTSIDE_SECRET" not in out
        assert "secret123" not in out
        assert "abc123" not in out
        assert "c1" not in out or "[REDACTED]" in out
        assert "[REDACTED]" in out

    @pytest.mark.parametrize(
        "text",
        [
            "primary_key=id_pk",
            "foreign_key=fk_ref",
            "tokenizer=t",
            "build finished in 3.2s, 12 files changed",
        ],
    )
    def test_string_kv_exemptions_preserved(self, text: str) -> None:
        # P1-4 回归: 误伤豁免在字符串形态下同样保留。
        assert redact_string(text) == text

    def test_shared_policy_may_fail_closed_on_token_assignment(self) -> None:
        assert redact_string("token_count=42") == "[REDACTED]"

    def test_string_kv_nested_message_redacted(self) -> None:
        # P1-4 回归: 嵌套 message 中的字符串凭据同样脱敏。
        data = {
            "message": "AWS_SECRET_ACCESS_KEY=OUTSIDE_SECRET",
            "nested": {"message": "DB_PASS=secret123"},
        }
        cleaned = sanitize_value(data)
        assert "OUTSIDE_SECRET" not in repr(cleaned)
        assert "secret123" not in repr(cleaned)

    def test_string_kv_cross_chunk_jsonl_redacted(self) -> None:
        # P1-4 回归: 跨 chunk JSONL 中的 message 凭据在组装后脱敏。
        line = '{"message": "client_secret=secret123"}\n'
        raw = line.encode()
        sink = Lines()
        asm = sink.assembler()
        mid = raw.index(b"secret123") - 2
        asm.feed(raw[:mid])
        asm.feed(raw[mid:])
        asm.flush()
        assert sink.lines == [line.strip()]
        cleaned = sanitize_value(json.loads(sink.lines[0]))
        assert "secret123" not in repr(cleaned)
        assert cleaned["message"] == "[REDACTED]"


class TestChildEnv:
    def test_whitelist_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_TOKEN", "leak-me")
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy:1")
        built = env_mod.build_child_env()
        joined = repr(built)
        assert "AGENT_TOKEN" not in joined
        assert "HTTPS_PROXY" not in joined

    def test_profile_keys_not_inherited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 复审回归: HOME/PATH 等用户 profile 键默认不得自动继承。
        monkeypatch.setenv("HOME", "/home/user-with-secrets")
        monkeypatch.setenv("PATH", "C:/user-profile/bin")
        monkeypatch.delenv("SystemRoot", raising=False)
        built = env_mod.build_child_env()
        assert "HOME" not in built
        assert "PATH" not in built

    def test_unknown_extra_key_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(env_mod.EnvPolicyError, match="not whitelisted"):
            env_mod.build_child_env({"EVIL_KEY": "x"})

    def test_explicit_whitelisted_key_accepted(self) -> None:
        # 每个平台都必须允许显式提供受控值(而非继承 profile)。
        keys = env_mod.allowed_keys()
        assert keys
        built = env_mod.build_child_env({keys[0]: "controlled-value"})
        assert built[keys[0]] == "controlled-value"

    def test_whitelisted_extra_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", "C:/system-bin")
        allowed = env_mod.allowed_keys()
        extra = {k: "v" for k in allowed}
        extra["PATH"] = "/controlled-only"
        built = env_mod.build_child_env(extra)
        assert built["PATH"] == "/controlled-only"

    def test_windows_required_systemroot_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SystemRoot", "C:\\WINDOWS")
        built = env_mod.build_child_env()
        if "SystemRoot" in env_mod.allowed_keys():
            assert built["SystemRoot"] == "C:\\WINDOWS"

    def test_value_length_cap_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = env_mod.allowed_keys()[0]
        with pytest.raises(env_mod.EnvPolicyError):
            env_mod.build_child_env({key: "v" * (env_mod.MAX_ENV_VALUE_CHARS + 1)})
