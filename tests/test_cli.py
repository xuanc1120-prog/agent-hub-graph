import json
from pathlib import Path

from typer.testing import CliRunner

from app.cli import app

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
