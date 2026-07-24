from __future__ import annotations

from pathlib import Path

import pytest

from master.router import (
    AgentCapability,
    AgentRouter,
    RoutingDecision,
)
from protocol import AssignmentMode, NodeType, TaskKind, WorkflowNode
from storage.agent_repository import AgentRegistration, AgentRepository
from storage.db import SCHEMA_VERSION, Database


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("auto_assignable", [False, True])
async def test_routing_state_round_trips_exactly(
    database: Database,
    *,
    enabled: bool,
    available: bool,
    auto_assignable: bool,
) -> None:
    registration = AgentRegistration(
        agent_id=f"agent-{int(enabled)}-{int(available)}-{int(auto_assignable)}",
        display_name="Round-trip Agent",
        adapter_type="opencode",
        capabilities=frozenset({AgentCapability.READ_CODE, AgentCapability.ANALYZE}),
        enabled=enabled,
        available=available,
        auto_assignable=auto_assignable,
        unavailable_reason=None if available else "probe failed",
    )
    repository = AgentRepository(database)

    registered = await repository.register(registration)
    loaded = await repository.get(registration.agent_id)
    catalog = await repository.catalog()

    assert loaded == registered
    assert catalog.find_by_id(registration.agent_id) == registered
    assert loaded.available is available
    assert loaded.auto_assignable is auto_assignable
    assert loaded.unavailable_reason == registration.unavailable_reason
    assert await repository.register(registration) == registered


async def test_reloaded_catalog_preserves_manual_and_auto_routing_policy(
    database: Database,
) -> None:
    repository = AgentRepository(database)
    await repository.register(
        AgentRegistration(
            agent_id="offline",
            display_name="Offline Agent",
            adapter_type="opencode",
            capabilities=frozenset({AgentCapability.READ_CODE, AgentCapability.ANALYZE}),
            available=False,
            auto_assignable=True,
            unavailable_reason="probe failed",
        )
    )
    await repository.register(
        AgentRegistration(
            agent_id="manual-only",
            display_name="Manual-only Agent",
            adapter_type="opencode",
            capabilities=frozenset({AgentCapability.READ_CODE, AgentCapability.ANALYZE}),
            available=True,
            auto_assignable=False,
        )
    )
    router = AgentRouter(await repository.catalog())

    automatic = router.route(
        WorkflowNode(
            id="auto",
            node_type=NodeType.AGENT_TASK,
            task_kind=TaskKind.ANALYZE,
            title="Auto",
            instruction="Analyze.",
        )
    )
    explicit = router.route(
        WorkflowNode(
            id="manual",
            node_type=NodeType.AGENT_TASK,
            task_kind=TaskKind.ANALYZE,
            title="Manual",
            instruction="Analyze.",
            assignment_mode=AssignmentMode.MANUAL,
            assigned_agent="offline",
        )
    )

    assert automatic.decision == RoutingDecision.BLOCKED_UNAVAILABLE
    assert explicit.decision == RoutingDecision.BLOCKED_UNAVAILABLE
    assert explicit.blocked_reason is not None
    assert "probe failed" in explicit.blocked_reason


async def test_v1_agent_rows_migrate_with_conservative_routing_defaults(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v1.db"
    migration = (Path(__file__).resolve().parents[2] / "migrations" / "init.sql").read_text(
        encoding="utf-8"
    )
    database = Database(path)
    async with database.connection() as connection:
        await connection.executescript(migration)
        await connection.execute(
            """
            INSERT INTO agents(
                id, display_name, adapter_type, enabled,
                capabilities_json, created_at
            ) VALUES (?, ?, ?, 1, '["read_code","analyze"]', ?)
            """,
            (
                "legacy-opencode",
                "Legacy OpenCode",
                "opencode",
                "2026-07-24T00:00:00.000000Z",
            ),
        )

    assert await database.initialize() == SCHEMA_VERSION
    loaded = await AgentRepository(database).get("legacy-opencode")

    assert loaded.available is False
    assert loaded.auto_assignable is False
    assert loaded.unavailable_reason is not None
