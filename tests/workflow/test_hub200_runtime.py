from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.services import WorkflowApplication
from protocol import (
    AssignmentMode,
    AuthorGraph,
    ChangeSetStatus,
    NodeRunStatus,
    NodeType,
    RiskLevel,
    TaskKind,
    WorkflowEdge,
    WorkflowLayout,
    WorkflowNode,
    WorkflowRunStatus,
)
from storage.errors import ConcurrencyConflict, ContainmentViolation
from storage.repositories import NewWorkflow


@pytest.mark.asyncio
async def test_mock_write_reaches_hub210_gate_and_restores_shared_repo(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    application = WorkflowApplication(Settings(data_dir=tmp_path / "agent-hub-data"))
    await application.initialize()
    await application.register_mock_agent()
    session = await application.create_session(
        repo=fixture_source_repo,
        goal="Create a bounded documentation demo file.",
        session_id="session-hub200",
    )
    workflow = await application.services.workflows.create(
        NewWorkflow(
            workflow_id="workflow-hub200",
            session_id=session.session_id,
            author_graph=_docs_write_graph(),
            layout=WorkflowLayout(),
        )
    )

    async with application.temporary_master() as lease:
        run = await application.run(
            workflow.workflow_id,
            lease=lease,
            workflow_run_id="run-hub200",
        )

    assert run.status == WorkflowRunStatus.BLOCKED
    assert application.services.git.state(session.shared_repo_path).dirty is False
    assert not (session.shared_repo_path / "docs" / "agent-hub-demo.md").exists()

    node_runs = await application.services.runs.list_nodes(run.workflow_run_id)
    by_type = {node.node_type: node for node in node_runs}
    assert by_type[NodeType.PATCH_GUARD].status == NodeRunStatus.COMPLETED
    assert by_type[NodeType.TEST].status == NodeRunStatus.COMPLETED
    assert by_type[NodeType.RISK_CLASSIFIER].status == NodeRunStatus.COMPLETED
    assert by_type[NodeType.APPROVAL].status == NodeRunStatus.BLOCKED_BY_GUARD

    async with application.services.database.connection() as connection:
        cursor = await connection.execute("SELECT id FROM change_sets")
        rows = await cursor.fetchall()
        await cursor.close()
    assert len(rows) == 1
    record = await application.services.change_sets.get(str(rows[0]["id"]))
    assert record.change_set.status == ChangeSetStatus.TEST_PASSED
    assert record.change_set.created_files == ["docs/agent-hub-demo.md"]
    assert record.change_set.patch_sha256 == record.change_set.canonical_patch_ref.sha256

    events = await application.show_events(workflow_run_id=run.workflow_run_id)
    change_events = [
        event for event in events if event.event_type == "workflow.change_set_state_changed"
    ]
    assert len(change_events) == 3

    async with application.temporary_master() as lease:
        with pytest.raises(ConcurrencyConflict):
            await application.services.change_sets.transition(
                record.change_set.change_set_id,
                expected=ChangeSetStatus.CAPTURED,
                target=ChangeSetStatus.GUARD_REJECTED,
                master_lease=lease,
            )

    patch_ref = record.change_set.canonical_patch_ref
    patch_path = application.services.artifacts.store.resolve(
        patch_ref.artifact_id,
        patch_ref.artifact_type.value,
    )
    patch_path.write_bytes(b"tampered patch")
    with pytest.raises(ContainmentViolation):
        await application.services.change_sets.load_patch(
            record.change_set.change_set_id,
        )


def _docs_write_graph() -> AuthorGraph:
    return AuthorGraph(
        nodes=[
            WorkflowNode(id="input", node_type=NodeType.INPUT, title="Input"),
            WorkflowNode(
                id="write-docs",
                node_type=NodeType.AGENT_TASK,
                task_kind=TaskKind.DOCS,
                title="Write demo documentation",
                instruction="Create the exact documentation file in the sealed scope.",
                assigned_agent="mock",
                assignment_mode=AssignmentMode.LOCKED,
                new_files_candidate=["docs/agent-hub-demo.md"],
                risk_level_hint=RiskLevel.L1,
                requires_write=True,
            ),
            WorkflowNode(id="output", node_type=NodeType.OUTPUT, title="Output"),
        ],
        edges=[
            WorkflowEdge(id="edge-input-write", from_node="input", to_node="write-docs"),
            WorkflowEdge(id="edge-write-output", from_node="write-docs", to_node="output"),
        ],
    )
