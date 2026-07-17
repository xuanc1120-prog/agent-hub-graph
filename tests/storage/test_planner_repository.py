from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from protocol import (
    AuthorGraph,
    NodeType,
    PlannerRunStatus,
    PlannerType,
    WorkflowEdge,
    WorkflowLayout,
    WorkflowNode,
)
from storage.db import Database
from storage.event_repository import EventRepository
from storage.leases import MasterLeaseRepository
from storage.planner_repository import PlannerRunRepository
from storage.repositories import (
    NewSession,
    NewWorkflow,
    SessionRepository,
    WorkflowRepository,
)
from workflow.events import build_runtime_event_registry


@pytest.mark.asyncio
async def test_planner_success_and_result_workflow_roll_back_together(
    database: Database,
    tmp_path: Path,
) -> None:
    session = await SessionRepository(database).create(
        NewSession(
            session_id="session-plan-atomic",
            goal="Plan atomically",
            source_repo_path=tmp_path / "source",
            shared_repo_path=tmp_path / "shared",
            base_commit="a" * 40,
            integration_branch="main",
            integration_head_commit="a" * 40,
        )
    )
    leases = MasterLeaseRepository(database)
    lease = await leases.acquire(
        instance_id="planner-master",
        process_id=2201,
        ttl_seconds=60,
    )
    events = EventRepository(database, build_runtime_event_registry())
    planners = PlannerRunRepository(database, events, leases)
    workflows = WorkflowRepository(database)
    await planners.create(
        planner_run_id="planner-atomic",
        session_id=session.session_id,
        planner_id="rule-based-planner",
        planner_type=PlannerType.RULE_BASED,
        integration_base_commit=session.integration_head_commit,
        lease=lease,
    )
    await planners.transition(
        "planner-atomic",
        expected=PlannerRunStatus.PENDING,
        target=PlannerRunStatus.RUNNING,
        lease=lease,
    )
    async with database.connection() as connection:
        cursor = await connection.executescript(
            """
            CREATE TRIGGER fail_planner_success_event
            BEFORE INSERT ON events
            WHEN NEW.event_type = 'workflow.planner_state_changed'
              AND json_extract(NEW.payload_json, '$.status') = 'succeeded'
            BEGIN
                SELECT RAISE(ABORT, 'injected planner success event failure');
            END;
            """
        )
        await cursor.close()

    with pytest.raises(aiosqlite.IntegrityError):
        await planners.succeed_with_workflow(
            "planner-atomic",
            workflow_repository=workflows,
            workflow=NewWorkflow(
                workflow_id="workflow-atomic",
                session_id=session.session_id,
                source_planner_run_id="planner-atomic",
                author_graph=_minimal_graph(),
                layout=WorkflowLayout(),
            ),
            lease=lease,
        )

    planner = await planners.get("planner-atomic")
    async with database.connection() as connection:
        cursor = await connection.execute("SELECT COUNT(*) AS count FROM workflows")
        workflow_count = int((await cursor.fetchone())["count"])
        await cursor.close()
    assert planner.status == PlannerRunStatus.RUNNING
    assert planner.result_workflow_id is None
    assert workflow_count == 0


def _minimal_graph() -> AuthorGraph:
    return AuthorGraph(
        nodes=[
            WorkflowNode(id="input", node_type=NodeType.INPUT, title="Input"),
            WorkflowNode(id="output", node_type=NodeType.OUTPUT, title="Output"),
        ],
        edges=[WorkflowEdge(id="edge", from_node="input", to_node="output")],
    )
