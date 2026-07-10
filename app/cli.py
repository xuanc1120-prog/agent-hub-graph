"""Typer command-line entry point for Agent Hub."""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from app import __version__
from app.config import DataPaths, Settings, ensure_data_directories

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)


def _settings(data_dir: Path | None = None) -> Settings:
    return Settings(data_dir=data_dir) if data_dir is not None else Settings()


@app.command("init-data")
def init_data(
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Override the validated runtime data root."),
    ] = None,
) -> None:
    """Create the external runtime data directory structure."""
    paths = ensure_data_directories(_settings(data_dir))
    typer.echo(str(paths.root))


@app.command()
def doctor(
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Inspect a specific runtime data root."),
    ] = None,
) -> None:
    """Print machine-readable baseline diagnostics without exposing secrets."""
    settings = _settings(data_dir)
    paths = DataPaths.from_settings(settings)
    payload = {
        "agent_hub_version": __version__,
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "data_dir": str(paths.root),
        "database_path": str(paths.database),
        "single_master": True,
    }
    typer.echo(json.dumps(payload, sort_keys=True))


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Listening host override.")] = None,
    port: Annotated[int | None, typer.Option(min=1, max=65535)] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Start the local API with exactly one worker."""
    settings = _settings(data_dir)
    ensure_data_directories(settings)
    uvicorn.run(
        "app.main:app",
        host=host or settings.api_host,
        port=port or settings.api_port,
        workers=1,
    )


if __name__ == "__main__":
    app()
