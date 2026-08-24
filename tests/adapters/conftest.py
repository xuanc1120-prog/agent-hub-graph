"""tests/adapters 共享夹具: 本地,无网络的 helper CLI。"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from adapters.cli.spec import CliAgentSpec

#: 独立 helper CLI 源码; 运行时写入 tmp_path,不依赖任何外部资源。
HELPER_SOURCE = '''
"""HUB-300 测试 helper: 无网络,无第三方依赖。"""
import json
import os
import subprocess
import sys
import time


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\\n")
    sys.stdout.flush()


def main(argv):
    mode = argv[0]
    if mode == "ok":
        emit({"event_type": "helper.started"})
        emit({"event_type": "helper.done", "code": 0})
        return 0
    if mode == "fail":
        code = int(argv[1])
        sys.stderr.write("intentional failure\\n")
        return code
    if mode == "frag":
        # 单个 JSON 对象分多次小块写出,含跨块多字节字符与键值拆分。
        parts = [
            b'{"event_type": "hel',
        ]
        obj = {"event_type": "helper.frag", "note": "\\u6d4b\\u8bd5-emoji-\\U0001f600"}
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        step = 7
        for i in range(0, len(raw), step):
            sys.stdout.buffer.write(raw[i : i + step])
            sys.stdout.buffer.flush()
            time.sleep(0.02)
        sys.stdout.buffer.write(b"\\n")
        sys.stdout.buffer.flush()
        del parts
        return 0
    if mode == "badline":
        emit({"event_type": "helper.before"})
        sys.stdout.write("token=TOP_SECRET_VALUE not-json\\n")
        emit({"event_type": "helper.after"})
        return 0
    if mode == "badutf8":
        sys.stdout.buffer.write(b"\\xff\\xfe\\n")
        sys.stdout.buffer.flush()
        emit({"event_type": "helper.after_utf8"})
        return 0
    if mode == "longline":
        sys.stdout.write("x" * 70000 + "\\n")
        sys.stdout.flush()
        emit({"event_type": "helper.after_long"})
        return 0
    if mode == "flood":
        payload = "y" * 4096
        while True:
            sys.stdout.write(payload + "\\n")
            sys.stdout.flush()
    if mode == "hang":
        if "--tree" in argv:
            grand = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"]
            )
            emit({"event_type": "helper.grandchild", "pid": grand.pid})
        time.sleep(120)
        return 0
    if mode == "envdump":
        # 以数组上报存在的变量名, 避免用环境变量名做 dict 键
        # (凭据形态的键名会被脱敏层整体替换)。
        names = argv[1].split(",")
        present = [n for n in names if n in os.environ]
        emit({"event_type": "helper.env", "present": present})
        return 0
    if mode == "secret":
        emit({"event_type": "helper.secret", "value": "api_key=SECRET_VALUE_123"})
        sys.stdout.write("plain token=TOP_SECRET_VALUE line\\n")
        return 0
    if mode == "fixedsecrets":
        values = [
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature1234",
            "postgresql://demo:synthetic-password@localhost/example",
            "-----BEGIN PRIVATE KEY-----\\nQUJDREVGR0g=\\n-----END PRIVATE KEY-----",
        ]
        raw = json.dumps(
            {
                "event_type": "totally.unknown_secret_kind",
                "nested": {"message": " | ".join(values)},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        for index in range(0, len(raw), 5):
            sys.stdout.buffer.write(raw[index : index + 5])
            sys.stdout.buffer.flush()
        sys.stdout.buffer.write(b"\\n")
        sys.stdout.buffer.flush()
        return 0
    if mode == "unknown":
        emit({"event_type": "totally.unknown_kind", "x": 1})
        return 0
    if mode == "emitpid":
        emit({"event_type": "helper.untrusted_pid", "pid": int(argv[1])})
        return 0
    if mode == "kvleak":
        # P1-4 回归: 复合凭据以字符串形态藏在 message/嵌套/未知事件中,
        # 必须使用与 dict 键相同的判定脱敏(含 =、:、export、引号值)。
        emit({"event_type": "helper.kv", "message": "AWS_SECRET_ACCESS_KEY=OUTSIDE_SECRET"})
        emit({"event_type": "helper.kv", "message": "DB_PASS=secret123"})
        emit({"event_type": "helper.kv", "message": "client_secret=secret123"})
        emit({"event_type": "helper.kv", "message": "oauth_token: secret123"})
        emit({"event_type": "helper.kv", "message": "export secret_access_key='secret123'"})
        emit({"event_type": "totally.unknown_kind", "message": "DB_PASS=secret123"})
        emit({"event_type": "helper.nested", "nested": {"message": "oauth_token=secret123"}})
        emit({"event_type": "helper.kv", "message": "primary_key=id_pk"})
        return 0
    if mode == "compositekeys":
        raw = json.dumps(
            {
                "event_type": "totally.unknown_composite_kind",
                "nested": {
                    "password_hash": "opaque-password-value",
                    "token_value": "opaque-token-value",
                    "api_key_value": "opaque-api-value",
                    "primary_password": "opaque-primary-value",
                },
            }
        ).encode() + b"\\n"
        split = raw.index(b"opaque-token-value") + 4
        sys.stdout.buffer.write(raw[:split])
        sys.stdout.buffer.flush()
        time.sleep(0.02)
        sys.stdout.buffer.write(raw[split:])
        sys.stdout.buffer.flush()
        return 0
    if mode == "echoprompt":
        emit({"event_type": "helper.prompt", "value": argv[1]})
        return 0
    if mode == "touch":
        with open(argv[1], "w", encoding="utf-8") as handle:
            handle.write("executed")
        return 0
    if mode == "burstexit":
        # 快速写大量事件后立即退出: 验证正常退出路径会排空管道尾部。
        for i in range(200):
            emit({"event_type": "helper.burst", "i": i})
        emit({"event_type": "helper.burst_done"})
        return 0
    if mode == "orphanexit":
        # 复审注入: 父进程打印事件后立即退出 0, 孙进程继承 stdout
        # 管道写端并长眠 -> 排空超时, 必须触发孤儿回收。
        emit({"event_type": "helper.parent_done"})
        if "--tree" in argv:
            grand = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"]
            )
            emit({"event_type": "helper.grandchild", "pid": grand.pid})
        return 0
    if mode == "orphanfail":
        grand = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"]
        )
        emit({"event_type": "helper.grandchild", "pid": grand.pid})
        return int(argv[1])
    if mode == "hangignore":
        import signal as sg

        sg.signal(sg.SIGTERM, sg.SIG_IGN)
        if "--tree" in argv:
            grand = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import signal,time;"
                    "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                    "print('READY',flush=True);time.sleep(120)",
                ],
                stdout=subprocess.PIPE,
            )
            assert grand.stdout is not None
            grand.stdout.readline()
            emit({"event_type": "helper.grandchild", "pid": grand.pid})
        time.sleep(120)
        return 0
    raise SystemExit("unknown mode")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''


@pytest.fixture
def helper_path(tmp_path: Path) -> Path:
    """把 helper CLI 写入临时目录并返回路径(本地,无网络)。"""

    path = tmp_path / "helper_agent.py"
    path.write_text(HELPER_SOURCE, encoding="utf-8")
    return path


@pytest.fixture
def make_spec(helper_path: Path) -> Callable[..., CliAgentSpec]:
    """以当前解释器为 executable 构造已注册身份的 spec。"""

    def _make(argv: list[str], **kwargs: object) -> CliAgentSpec:
        return CliAgentSpec.register(
            executable=sys.executable,
            argv=[str(helper_path), *argv],
            **kwargs,
        )

    return _make
