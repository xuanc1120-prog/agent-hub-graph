"""Typer command-line entry point for Agent Hub."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import sys
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from app import __version__
from app.config import DataPaths, Settings, ensure_data_directories
from storage.db import Database
from storage.errors import LeaseLost, LeaseUnavailable
from storage.leases import MasterLease, MasterLeaseRepository

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


@app.command("init-db")
def init_db(
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Override the validated runtime data root."),
    ] = None,
) -> None:
    """Create or verify the versioned SQLite schema."""
    paths = ensure_data_directories(_settings(data_dir))
    version = asyncio.run(Database(paths.database).initialize())
    typer.echo(json.dumps({"database": str(paths.database), "schema_version": version}))


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
    try:
        asyncio.run(
            _serve_application(
                settings,
                host=host or settings.api_host,
                port=port or settings.api_port,
            )
        )
    except LeaseUnavailable as error:
        typer.echo(f"Agent Hub Master is already running: {error}", err=True)
        raise typer.Exit(code=2) from error


async def _serve_application(settings: Settings, *, host: str, port: int) -> None:
    paths = ensure_data_directories(settings)
    database = Database(paths.database)
    await database.initialize()
    leases = MasterLeaseRepository(database)
    lease = await leases.acquire(
        instance_id=f"master-{uuid.uuid4().hex}",
        process_id=os.getpid(),
        ttl_seconds=settings.master_lease_ttl_seconds,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            "app.main:app",
            host=host,
            port=port,
            workers=1,
            proxy_headers=False,
        )
    )
    heartbeat = asyncio.create_task(_heartbeat_master_lease(leases, lease, settings, server))
    try:
        await server.serve()
        if heartbeat.done():
            heartbeat.result()
    finally:
        server.should_exit = True
        if not heartbeat.done():
            heartbeat.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await heartbeat
        finally:
            with suppress(LeaseLost):
                await leases.release(lease)


async def _heartbeat_master_lease(
    repository: MasterLeaseRepository,
    lease: MasterLease,
    settings: Settings,
    server: uvicorn.Server,
) -> None:
    current = lease
    interval = min(
        settings.lease_heartbeat_seconds,
        max(1, settings.master_lease_ttl_seconds // 3),
    )
    try:
        while not server.should_exit:
            await asyncio.sleep(interval)
            current = await repository.heartbeat(
                current,
                ttl_seconds=settings.master_lease_ttl_seconds,
            )
    except Exception:
        server.should_exit = True
        raise


if __name__ == "__main__":
    app()
