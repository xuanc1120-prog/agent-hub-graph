import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from app.cli import app
from storage.db import Database
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
    assert payload["schema_version"] == 1
    connection = sqlite3.connect(data_root / "agent-hub.db")
    version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    connection.close()
    assert version == 1


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
    replayed = runner.invoke(
        app,
        ["show-events", "--workflow-run-id", "run-cli", *common],
    )

    for result in (registered, created, planned, validated, executed, replayed):
        assert result.exit_code == 0, result.stdout
    assert json.loads(created.stdout)["session_id"] == "session-cli"
    assert json.loads(planned.stdout)["demo_read_only"] is True
    assert json.loads(validated.stdout)["ok"] is True
    assert json.loads(executed.stdout)["status"] == "completed"
    events = json.loads(replayed.stdout)
    assert events[0]["run_seq"] == 1
    assert events[-1]["payload"]["status"] == "completed"


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
