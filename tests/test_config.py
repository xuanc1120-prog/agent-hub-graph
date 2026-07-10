from pathlib import Path

from app.config import DataPaths, Settings, ensure_data_directories


def test_data_paths_are_rooted_under_configured_directory(tmp_path: Path) -> None:
    data_root = tmp_path / "agent-hub-data"
    settings = Settings(data_dir=data_root)

    paths = ensure_data_directories(settings)

    assert paths == DataPaths.from_settings(settings)
    assert paths.database == data_root / "agent-hub.db"
    assert all(path.is_dir() for path in paths.directories())
    assert paths.opencode_profile == data_root / "profiles" / "opencode"


def test_config_rejects_multiple_master_worker_configuration() -> None:
    fields = Settings.model_fields

    assert "api_workers" not in fields
    assert Settings().scheduler_poll_ms == 250
