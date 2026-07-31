from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from security.command_guard import ApprovedCommand, CommandGuard
from security.test_runner import (
    TestRunner as SafeTestRunner,
)
from security.test_runner import (
    _bounded_utf8,
    _OutputCollector,
    _redact_output,
)


@pytest.mark.asyncio
async def test_runner_uses_minimal_environment_and_bounded_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    tests = repo / "tests"
    tests.mkdir(parents=True)
    (tests / "test_environment.py").write_text(
        "import os\n\n"
        "def test_environment():\n"
        "    assert 'AGENT_HUB_DEMO_TOKEN' not in os.environ\n"
        "    print('x' * 2000)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_HUB_DEMO_TOKEN", "must-not-reach-child")
    runner = SafeTestRunner(CommandGuard(), timeout_seconds=30, max_output_bytes=64)

    result = await runner.run(
        ["pytest", "-q", "tests/test_environment.py"],
        repo=repo,
        runtime_directory=tmp_path / "runtime",
    )

    assert result.passed
    assert result.output_truncated
    assert "must-not-reach-child" not in result.output
    assert len(result.output.encode("utf-8")) <= 64


@pytest.mark.asyncio
async def test_runner_times_out_and_reaps_process_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    tests = repo / "tests"
    tests.mkdir(parents=True)
    (tests / "test_timeout.py").write_text(
        "import time\n\ndef test_timeout():\n    time.sleep(30)\n",
        encoding="utf-8",
    )
    runner = SafeTestRunner(CommandGuard(), timeout_seconds=1, max_output_bytes=10_000)

    result = await runner.run(
        ["pytest", "-q", "tests/test_timeout.py"],
        repo=repo,
        runtime_directory=tmp_path / "runtime",
    )

    assert result.timed_out
    assert not result.passed


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher symlink behavior")
def test_runner_preserves_package_manager_launcher_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "npm-cli.js"
    target.write_text("", encoding="utf-8")
    launcher = tmp_path / "npm"
    launcher.symlink_to(target)
    monkeypatch.setattr("security.test_runner.shutil.which", lambda _: str(launcher))

    executable, arguments = SafeTestRunner._resolve(ApprovedCommand("npm-test", ("npm", "test")))

    assert executable == str(launcher.absolute())
    assert arguments == ("test",)


def test_runner_adds_node_runtime_to_package_manager_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    npm_directory = tmp_path / "npm-bin"
    node_directory = tmp_path / "node-bin"
    npm_directory.mkdir()
    node_directory.mkdir()
    npm = npm_directory / ("npm.cmd" if os.name == "nt" else "npm")
    node = node_directory / ("node.exe" if os.name == "nt" else "node")
    npm.touch()
    node.touch()
    monkeypatch.setattr(
        "security.test_runner.shutil.which",
        lambda name: str(node) if name == "node" else None,
    )

    environment = SafeTestRunner._environment(
        tmp_path / "runtime",
        str(npm),
        "npm-test",
    )

    assert environment["PATH"].split(os.pathsep) == [
        str(npm_directory),
        str(node_directory),
    ]


def test_runner_rejects_package_manager_without_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("security.test_runner.shutil.which", lambda _: None)

    with pytest.raises(FileNotFoundError, match=r"requires an available Node\.js"):
        SafeTestRunner._environment(
            tmp_path / "runtime",
            str(tmp_path / "npm"),
            "npm-test",
        )


@pytest.mark.asyncio
async def test_output_redaction_spans_reader_chunks_and_output_boundary() -> None:
    reader = asyncio.StreamReader()
    collector = _OutputCollector(limit=256)
    secret = "split-secret-token-value"
    reader.feed_data(b"x" * 60 + b" Bearer split-sec")
    reader.feed_data(b"ret-token-value tail")
    reader.feed_eof()

    await collector.read(reader)
    redacted = _redact_output(collector.value.decode("utf-8"))
    bounded, truncated = _bounded_utf8(redacted, 64)

    assert secret not in redacted
    assert secret not in bounded
    assert "[REDACTED]" in redacted
    assert truncated is True
    assert len(bounded.encode("utf-8")) <= 64


@pytest.mark.parametrize(
    "value,secret",
    [
        (
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
            "abcdefghijklmnopqrstuvwxyz012345",
        ),
        (
            "token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZ2VudC1odWIifQ.c2lnbmF0dXJlLXZhbHVl",
            "eyJhbGciOiJIUzI1NiJ9",
        ),
        (
            "DATABASE_URL=postgresql://agent:secret-password@localhost/demo",
            "secret-password",
        ),
        (
            "DATABASE_URL=postgresql+psycopg://agent:driver-password@db/app",
            "driver-password",
        ),
        (
            "DATABASE_URL=mysql+pymysql://agent:mysql-password@db/app",
            "mysql-password",
        ),
        (
            "DATABASE_URL=mssql+pyodbc://agent:mssql-password@db/app",
            "mssql-password",
        ),
        (
            "DATABASE_URL=customdb://agent:fallback-password@db/app",
            "fallback-password",
        ),
        (
            "sqlite+pysqlite://agent:sqlite-password@localhost/app",
            "sqlite-password",
        ),
        (
            "oracle+cx_oracle://agent:oracle-password@db/app",
            "oracle-password",
        ),
        (
            "cockroachdb+psycopg://agent:roach-password@db/app",
            "roach-password",
        ),
        (
            "sqlalchemy.url = oracle+cx_oracle://agent:ini-password@db/app",
            "ini-password",
        ),
        (
            'token = "cioabcdefghijklmnopqrstuvwxyz0123456789"',
            "cioabcdefghijklmnopqrstuvwxyz0123456789",
        ),
        (
            '"oauth_token": "oauth-value-that-must-be-redacted"',
            "oauth-value-that-must-be-redacted",
        ),
        (
            '{"accessToken":"camel-case-secret-value"}',
            "camel-case-secret-value",
        ),
        (
            "<server><password>maven-password-value</password></server>",
            "maven-password-value",
        ),
        (
            "repositoryPassword=gradle-password-value",
            "gradle-password-value",
        ),
        ("api_key=top-secret-value", "top-secret-value"),
        (
            'export AWS_SECRET_ACCESS_KEY="aws-output-secret-value"',
            "aws-output-secret-value",
        ),
        ('DB_PASS="database-output-password"', "database-output-password"),
    ],
)
def test_structured_credentials_are_redacted(value: str, secret: str) -> None:
    redacted = _redact_output(value)

    assert secret not in redacted
    assert "[REDACTED]" in redacted


@pytest.mark.asyncio
async def test_normalized_assignment_redaction_spans_reader_chunks() -> None:
    reader = asyncio.StreamReader()
    collector = _OutputCollector(limit=512)
    reader.feed_data(b"prefix export AWS_SECRET_ACC")
    reader.feed_data(b'ESS_KEY="cross-chunk-aws-secret" suffix')
    reader.feed_eof()

    await collector.read(reader)
    redacted = _redact_output(collector.value.decode("utf-8"))

    assert "cross-chunk-aws-secret" not in redacted
    assert "AWS_SECRET_ACCESS_KEY" not in redacted
    assert "[REDACTED]" in redacted


@pytest.mark.asyncio
async def test_driver_database_url_redaction_spans_reader_chunks() -> None:
    reader = asyncio.StreamReader()
    collector = _OutputCollector(limit=512)
    reader.feed_data(b"DATABASE_URL=postgresql+psy")
    reader.feed_data(b"copg://agent:chunk-password@db/app")
    reader.feed_eof()

    await collector.read(reader)
    redacted = _redact_output(collector.value.decode("utf-8"))

    assert "chunk-password" not in redacted
    assert "postgresql+psycopg://" not in redacted
    assert "[REDACTED]" in redacted


@pytest.mark.asyncio
async def test_bare_unknown_dialect_url_redaction_spans_reader_chunks() -> None:
    reader = asyncio.StreamReader()
    collector = _OutputCollector(limit=512)
    reader.feed_data(b"oracle+cx_oracle://agent:cross-")
    reader.feed_data(b"chunk-password@db/app")
    reader.feed_eof()

    await collector.read(reader)
    redacted = _redact_output(collector.value.decode("utf-8"))

    assert "cross-chunk-password" not in redacted
    assert "oracle+cx_oracle://" not in redacted
    assert "[REDACTED]" in redacted


@pytest.mark.asyncio
async def test_private_key_redaction_spans_chunks_before_bounding() -> None:
    reader = asyncio.StreamReader()
    collector = _OutputCollector(limit=1024)
    reader.feed_data(b"prefix -----BEGIN OPENSSH PRI")
    reader.feed_data(b"VATE KEY-----\nsuper-secret-key-material\n")
    reader.feed_data(b"-----END OPENSSH PRIVATE KEY----- suffix")
    reader.feed_eof()

    await collector.read(reader)
    redacted = _redact_output(collector.value.decode("utf-8"))
    bounded, _truncated = _bounded_utf8(redacted, 64)

    assert "super-secret-key-material" not in redacted
    assert "super-secret-key-material" not in bounded
    assert "[REDACTED]" in redacted
