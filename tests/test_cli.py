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
