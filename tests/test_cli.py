import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import app.cli as cli_module
from app.cli import app
from app.config import Settings
from app.services import WorkflowApplication
from storage.db import SCHEMA_VERSION, Database
from storage.errors import LeaseLost
from storage.leases import MasterLeaseRepository

runner = CliRunner()


def test_init_data_and_doctor(tmp_path: Path) -> None:
    data_root = tmp_path / "runtime"

    init_result = runner.invoke(app, ["init-data", "--data-dir", str(data_root)])
    doctor_result = runner.invoke(app, ["doctor", "--data-dir", str(data_root)])

    assert init_result.exit_code == 0
    assert data_root.is_dir()
    assert doctor_result.exit_code == 0
    payload = json.loads(doctor_result.stdout)
    assert payload["data_dir"] == str(data_root.resolve())
    assert payload["single_master"] is True


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    data_root = tmp_path / "runtime"

    first = runner.invoke(app, ["init-db", "--data-dir", str(data_root)])
    second = runner.invoke(app, ["init-db", "--data-dir", str(data_root)])

    assert first.exit_code == 0
    assert second.exit_code == 0
    payload = json.loads(first.stdout)
    assert payload["schema_version"] == SCHEMA_VERSION
    connection = sqlite3.connect(data_root / "agent-hub.db")
    version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    connection.close()
    assert version == SCHEMA_VERSION


def test_serve_fails_fast_when_master_lease_is_held(tmp_path: Path) -> None:
    data_root = tmp_path / "runtime"
    database = Database(data_root / "agent-hub.db")

    async def hold_lease() -> None:
        await database.initialize()
        await MasterLeaseRepository(database).acquire(
            instance_id="existing-master",
            process_id=999,
            ttl_seconds=60,
            now=datetime.now(UTC),
        )

    asyncio.run(hold_lease())
    result = runner.invoke(app, ["serve", "--data-dir", str(data_root), "--port", "9876"])

    assert result.exit_code == 2
    assert "already running" in result.stderr


def test_cli_readonly_mock_phase_gate(fixture_source_repo: Path, tmp_path: Path) -> None:
    data_root = tmp_path / "runtime"
    common = ["--data-dir", str(data_root)]

    registered = runner.invoke(app, ["register-agent", "mock", *common])
    created = runner.invoke(
        app,
        [
            "create-session",
            "--repo",
            str(fixture_source_repo),
            "--goal",
            "Fix a fixture bug without changing files in the phase-one demo",
            "--session-id",
            "session-cli",
            *common,
        ],
    )
    planned = runner.invoke(
        app,
        [
            "plan",
            "session-cli",
            "--task-family",
            "bugfix",
            "--planner-run-id",
            "planner-cli",
            "--workflow-id",
            "workflow-cli",
            *common,
        ],
    )
    validated = runner.invoke(app, ["validate", "workflow-cli", *common])
    executed = runner.invoke(
        app,
        [
            "run-workflow",
            "workflow-cli",
            "--workflow-run-id",
            "run-cli",
            *common,
        ],
    )
    shown = runner.invoke(app, ["show-run", "run-cli", *common])
    replayed = runner.invoke(
        app,
        ["show-events", "--workflow-run-id", "run-cli", "--limit", "2", *common],
    )

    for result in (registered, created, planned, validated, executed, shown, replayed):
        assert result.exit_code == 0, result.stdout
    assert json.loads(created.stdout)["session_id"] == "session-cli"
    assert json.loads(planned.stdout)["demo_read_only"] is True
    assert json.loads(validated.stdout)["ok"] is True
    assert json.loads(executed.stdout)["status"] == "completed"
    run = json.loads(shown.stdout)
    assert run["planner_run_id"] == "planner-cli"
    assert run["planner_id"] == "rule-based-planner"
    assert run["planner_model"] is None
    events = json.loads(replayed.stdout)
    assert len(events) > 2
    assert events[0]["run_seq"] == 1
    assert events[-1]["payload"]["status"] == "completed"
    run_created = next(event for event in events if event["event_type"] == "workflow.run_created")
    assert run_created["payload"]["planner_run_id"] == "planner-cli"
    assert run_created["payload"]["planner_id"] == "rule-based-planner"
    assert run_created["payload"]["planner_model"] is None


def test_create_session_rejects_path_like_id_before_workspace_creation(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "runtime"
    escaped = tmp_path / "escaped-session"

    result = runner.invoke(
        app,
        [
            "create-session",
            "--repo",
            str(fixture_source_repo),
            "--goal",
            "Validate session id containment",
            "--session-id",
            "../../escaped-session",
            "--data-dir",
            str(data_root),
        ],
    )

    assert result.exit_code != 0
    assert not escaped.exists()
    shared_root = data_root / "workspaces" / "shared"
    assert not shared_root.exists() or list(shared_root.iterdir()) == []


class _FakeServer:
    def __init__(self, _config: object) -> None:
        self.should_exit = False
        self.serve_called = False

    async def serve(self) -> None:
        self.serve_called = True


def _install_fake_serve_runtime(
    monkeypatch: pytest.MonkeyPatch,
    service: WorkflowApplication,
) -> list[_FakeServer]:
    servers: list[_FakeServer] = []

    def build_server(config: object) -> _FakeServer:
        server = _FakeServer(config)
        servers.append(server)
        return server

    monkeypatch.setattr(cli_module, "WorkflowApplication", lambda _settings: service)
    monkeypatch.setattr(cli_module.uvicorn, "Server", build_server)
    return servers


@pytest.mark.asyncio
async def test_serve_renews_master_lease_during_slow_startup_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "slow-recovery",
        master_lease_ttl_seconds=5,
        lease_heartbeat_seconds=1,
    )
    service = WorkflowApplication(settings)
    servers = _install_fake_serve_runtime(monkeypatch, service)
    leases = service.services.leases
    original_heartbeat = leases.heartbeat
    heartbeat_calls = 0
    recovery_finished = asyncio.Event()

    async def tracked_heartbeat(*args: object, **kwargs: object) -> object:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        return await original_heartbeat(*args, **kwargs)

    async def slow_recovery(*, lease: object) -> list[object]:
        await asyncio.sleep(5.2)
        await leases.assert_valid(lease)
        recovery_finished.set()
        return []

    monkeypatch.setattr(leases, "heartbeat", tracked_heartbeat)
    monkeypatch.setattr(service.services.recovery, "recover", slow_recovery)

    await cli_module._serve_application(settings, host="127.0.0.1", port=9876)

    assert recovery_finished.is_set()
    assert heartbeat_calls >= 4
    assert len(servers) == 1 and servers[0].serve_called


@pytest.mark.asyncio
async def test_serve_heartbeat_failure_cancels_startup_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "heartbeat-failure",
        master_lease_ttl_seconds=5,
        lease_heartbeat_seconds=1,
    )
    service = WorkflowApplication(settings)
    servers = _install_fake_serve_runtime(monkeypatch, service)
    recovery_started = asyncio.Event()
    recovery_cancelled = asyncio.Event()
    heartbeat_failed = asyncio.Event()

    async def fail_heartbeat(*_args: object, **_kwargs: object) -> object:
        heartbeat_failed.set()
        raise LeaseLost("injected heartbeat failure")

    async def blocked_recovery(*, lease: object) -> list[object]:
        recovery_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            recovery_cancelled.set()
            raise

    monkeypatch.setattr(service.services.leases, "heartbeat", fail_heartbeat)
    monkeypatch.setattr(service.services.recovery, "recover", blocked_recovery)

    with pytest.raises(LeaseLost, match="injected heartbeat failure"):
        await cli_module._serve_application(settings, host="127.0.0.1", port=9876)

    assert recovery_started.is_set()
    assert heartbeat_failed.is_set()
    assert recovery_cancelled.is_set()
    assert not servers[0].serve_called
    replacement = await service.services.leases.acquire(
        instance_id="replacement-after-heartbeat-failure",
        process_id=1234,
        ttl_seconds=5,
    )
    await service.services.leases.release(replacement)


@pytest.mark.asyncio
async def test_serve_cancellation_during_recovery_releases_master_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "startup-cancel",
        master_lease_ttl_seconds=5,
        lease_heartbeat_seconds=1,
    )
    service = WorkflowApplication(settings)
    servers = _install_fake_serve_runtime(monkeypatch, service)
    recovery_started = asyncio.Event()
    recovery_cancelled = asyncio.Event()

    async def blocked_recovery(*, lease: object) -> list[object]:
        recovery_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            recovery_cancelled.set()
            raise

    monkeypatch.setattr(service.services.recovery, "recover", blocked_recovery)
    serving = asyncio.create_task(
        cli_module._serve_application(settings, host="127.0.0.1", port=9876)
    )
    await asyncio.wait_for(recovery_started.wait(), timeout=3)
    serving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serving

    assert recovery_cancelled.is_set()
    assert not servers[0].serve_called
    replacement = await service.services.leases.acquire(
        instance_id="replacement-after-startup-cancel",
        process_id=1234,
        ttl_seconds=5,
    )
    await service.services.leases.release(replacement)
