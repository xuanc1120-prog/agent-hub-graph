"""Typer command-line entry point for Agent Hub."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import secrets
import sys
import uuid
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from app import __version__
from app.config import DataPaths, Settings, ensure_data_directories, validate_loopback_host
from app.main import create_app
from app.services import WorkflowApplication
from master.planner import TemplateKind
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


@app.command("register-agent")
def register_agent(
    agent_id: Annotated[str, typer.Argument(help="Agent id; HUB-110 supports only mock.")] = "mock",
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Register the deterministic read-only MockAgent."""
    if agent_id != "mock":
        raise typer.BadParameter("HUB-110 supports only the mock agent")

    async def operation() -> None:
        service = WorkflowApplication(_settings(data_dir))
        await service.initialize()
        await service.register_mock_agent()

    asyncio.run(operation())
    typer.echo(json.dumps({"agent_id": "mock", "registered": True}))


@app.command("create-session")
def create_session(
    repo: Annotated[Path, typer.Option("--repo", help="Clean source Git repository.")],
    goal: Annotated[str, typer.Option("--goal", help="Session goal.")],
    session_id: Annotated[str | None, typer.Option("--session-id")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Create a session pinned to the current clean Git commit."""

    async def operation():
        service = WorkflowApplication(_settings(data_dir))
        await service.initialize()
        return await service.create_session(repo=repo, goal=goal, session_id=session_id)

    session = asyncio.run(operation())
    typer.echo(
        json.dumps(
            {
                "session_id": session.session_id,
                "base_commit": session.base_commit,
                "integration_branch": session.integration_branch,
            },
            sort_keys=True,
        )
    )


@app.command("plan")
def plan_workflow(
    session_id: Annotated[str, typer.Argument(help="Session id.")],
    task_family: Annotated[
        TemplateKind,
        typer.Option("--task-family", help="Deterministic RuleBasedPlanner template."),
    ] = TemplateKind.BUGFIX,
    full_template: Annotated[
        bool,
        typer.Option("--full-template", help="Keep write tasks for preview; not executable yet."),
    ] = False,
    planner_run_id: Annotated[str | None, typer.Option("--planner-run-id")] = None,
    workflow_id: Annotated[str | None, typer.Option("--workflow-id")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Plan a workflow; default output is the safe read-only Mock demo slice."""

    async def operation():
        service = WorkflowApplication(_settings(data_dir))
        await service.initialize()
        await service.register_mock_agent()
        async with service.temporary_master() as lease:
            return await service.plan(
                session_id,
                task_family=task_family,
                lease=lease,
                full_template=full_template,
                planner_run_id=planner_run_id,
                workflow_id=workflow_id,
            )

    result = asyncio.run(operation())
    typer.echo(json.dumps(asdict(result), sort_keys=True))


@app.command("validate")
def validate_workflow(
    workflow_id: Annotated[str, typer.Argument(help="Workflow id.")],
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Compile and validate a workflow against the current snapshot tuple."""

    async def operation():
        service = WorkflowApplication(_settings(data_dir))
        await service.initialize()
        return await service.validate(workflow_id)

    result = asyncio.run(operation())
    typer.echo(result.model_dump_json())
    if not result.ok:
        raise typer.Exit(code=2)


@app.command("show-workflow")
def show_workflow(
    workflow_id: Annotated[str, typer.Argument(help="Workflow id.")],
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Show current author/layout versions and the last valid compile preview."""

    async def operation():
        service = WorkflowApplication(_settings(data_dir))
        await service.initialize()
        return await service.show_workflow(workflow_id)

    workflow = asyncio.run(operation())
    typer.echo(
        json.dumps(
            {
                "workflow_id": workflow.workflow_id,
                "session_id": workflow.session_id,
                "semantic_version": workflow.semantic_version,
                "layout_version": workflow.layout_version,
                "author_graph": workflow.author_graph.model_dump(mode="json"),
                "layout": workflow.layout.model_dump(mode="json"),
                "last_compiled_graph_hash": workflow.last_compiled_graph_hash,
            },
            sort_keys=True,
        )
    )


@app.command("show-run")
def show_run(
    workflow_run_id: Annotated[str, typer.Argument(help="Workflow run id.")],
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Show the immutable run snapshot identity and planner lineage."""

    async def operation():
        service = WorkflowApplication(_settings(data_dir))
        await service.initialize()
        return await service.show_run(workflow_run_id)

    run = asyncio.run(operation())
    typer.echo(
        json.dumps(
            {
                "workflow_run_id": run.workflow_run_id,
                "workflow_id": run.workflow_id,
                "session_id": run.session_id,
                "status": run.status.value,
                "author_snapshot_hash": run.author_snapshot_hash,
                "compiled_snapshot_hash": run.compiled_snapshot_hash,
                "agent_catalog_snapshot_hash": run.agent_catalog_snapshot_hash,
                "planner_run_id": run.planner_run_id,
                "planner_id": run.planner_id,
                "planner_model": run.planner_model,
            },
            sort_keys=True,
        )
    )


@app.command("run-workflow")
def run_workflow(
    workflow_id: Annotated[str, typer.Argument(help="Workflow id.")],
    confirmed_compiled_hash: Annotated[
        str | None,
        typer.Option("--confirmed-compiled-hash"),
    ] = None,
    workflow_run_id: Annotated[str | None, typer.Option("--workflow-run-id")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Run a validated no-side-effect workflow through DurableScheduler."""

    async def operation():
        service = WorkflowApplication(_settings(data_dir))
        await service.initialize()
        async with service.temporary_master() as lease:
            return await service.run(
                workflow_id,
                lease=lease,
                confirmed_compiled_hash=confirmed_compiled_hash,
                workflow_run_id=workflow_run_id,
            )

    run = asyncio.run(operation())
    typer.echo(
        json.dumps(
            {
                "workflow_run_id": run.workflow_run_id,
                "status": run.status.value,
                "compiled_snapshot_hash": run.compiled_snapshot_hash,
                "planner_run_id": run.planner_run_id,
                "planner_id": run.planner_id,
                "planner_model": run.planner_model,
            },
            sort_keys=True,
        )
    )


@app.command("show-events")
def show_events(
    workflow_run_id: Annotated[str | None, typer.Option("--workflow-run-id")] = None,
    session_id: Annotated[str | None, typer.Option("--session-id")] = None,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Replay validated typed events for one run or session."""

    async def operation():
        service = WorkflowApplication(_settings(data_dir))
        await service.initialize()
        return await service.show_events(
            workflow_run_id=workflow_run_id,
            session_id=session_id,
            limit=limit,
        )

    events = asyncio.run(operation())
    typer.echo(
        json.dumps(
            [
                {
                    "event_id": event.event_id,
                    "run_seq": event.run_seq,
                    "event_type": event.event_type,
                    "payload": json.loads(event.payload_json),
                    "created_at": event.created_at,
                }
                for event in events
            ],
            sort_keys=True,
        )
    )


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
    resolved_host = validate_loopback_host(host)
    runtime_values = settings.model_dump(mode="python")
    runtime_values.update({"api_host": resolved_host, "api_port": port})
    runtime_settings = Settings.model_validate(runtime_values)
    service = WorkflowApplication(runtime_settings)
    await service.initialize()
    api_token = secrets.token_urlsafe(32)
    application = create_app(
        runtime_settings,
        workflow_application=service,
        master_lease=None,
        api_token=api_token,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host=resolved_host,
            port=port,
            workers=1,
            proxy_headers=False,
            access_log=False,
        )
    )
    leases = service.services.leases
    lease = await leases.acquire(
        instance_id=f"master-{uuid.uuid4().hex}",
        process_id=os.getpid(),
        ttl_seconds=runtime_settings.master_lease_ttl_seconds,
    )
    application.state.master_lease = lease
    scheduler_stop = asyncio.Event()
    heartbeat: asyncio.Task[None] | None = None
    scheduler: asyncio.Task[None] | None = None
    startup: asyncio.Task[None] | None = None
    try:
        heartbeat = asyncio.create_task(
            _heartbeat_master_lease(leases, lease, runtime_settings, server)
        )
        startup = asyncio.create_task(_initialize_runtime(service, lease))
        await _await_startup_with_heartbeat(startup, heartbeat)
        typer.echo(f"Agent Hub API bearer token (shown once): {api_token}", err=True)
        scheduler = asyncio.create_task(_poll_scheduler(service, lease, scheduler_stop, server))
        await server.serve()
    finally:
        server.should_exit = True
        scheduler_stop.set()
        if heartbeat is not None and not heartbeat.done():
            heartbeat.cancel()
        if startup is not None and not startup.done():
            startup.cancel()
        background_tasks = [task for task in (heartbeat, scheduler, startup) if task is not None]
        background_results = await asyncio.gather(*background_tasks, return_exceptions=True)
        with suppress(LeaseLost):
            await leases.release(lease)
    for result in background_results:
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            raise result


async def _initialize_runtime(service: WorkflowApplication, lease: MasterLease) -> None:
    await service.register_mock_agent()
    await service.services.recovery.recover(lease=lease)


async def _await_startup_with_heartbeat(
    startup: asyncio.Task[None],
    heartbeat: asyncio.Task[None],
) -> None:
    done, _pending = await asyncio.wait(
        (startup, heartbeat),
        return_when=asyncio.FIRST_COMPLETED,
    )
    if heartbeat in done:
        await heartbeat
        raise RuntimeError("Master heartbeat stopped during startup.")
    await startup


async def _poll_scheduler(
    service: WorkflowApplication,
    lease: MasterLease,
    stop: asyncio.Event,
    server: uvicorn.Server,
) -> None:
    try:
        await service.services.scheduler.poll(lease=lease, stop=stop)
    except Exception:
        server.should_exit = True
        raise


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
