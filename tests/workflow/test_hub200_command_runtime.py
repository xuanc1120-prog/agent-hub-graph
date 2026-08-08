from __future__ import annotations

import json
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
from storage.repositories import NewWorkflow


@pytest.mark.asyncio
async def test_mock_command_test_reaches_approval_and_restores_shared_repo(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    application = WorkflowApplication(Settings(data_dir=tmp_path / "agent-hub-data"))
    await application.initialize()
    await application.register_mock_agent()
    session = await application.create_session(
        repo=fixture_source_repo,
        goal="Create and run one sealed passing test.",
        session_id="session-hub200-command",
    )
    workflow = await application.services.workflows.create(
        NewWorkflow(
            workflow_id="workflow-hub200-command",
            session_id=session.session_id,
            author_graph=_command_write_graph(),
            layout=WorkflowLayout(),
        )
    )

    async with application.temporary_master() as lease:
        run = await application.run(
            workflow.workflow_id,
            lease=lease,
            workflow_run_id="run-hub200-command",
        )

    assert run.status == WorkflowRunStatus.BLOCKED
    assert application.services.git.state(session.shared_repo_path).dirty is False
    assert not (session.shared_repo_path / "tests" / "test_agent_hub_demo.py").exists()

    node_runs = await application.services.runs.list_nodes(run.workflow_run_id)
    by_type = {node.node_type: node for node in node_runs}
    assert by_type[NodeType.PATCH_GUARD].status == NodeRunStatus.COMPLETED
    assert by_type[NodeType.COMMAND_GUARD].status == NodeRunStatus.COMPLETED
    assert by_type[NodeType.TEST].status == NodeRunStatus.COMPLETED
    assert by_type[NodeType.RISK_CLASSIFIER].status == NodeRunStatus.COMPLETED
    assert by_type[NodeType.APPROVAL].status == NodeRunStatus.BLOCKED_BY_GUARD

    record = await application.services.change_sets.get_for_source_node(
        workflow_run_id=run.workflow_run_id,
        source_node_id="write-test",
    )
    assert record.change_set.status == ChangeSetStatus.TEST_PASSED
    assert record.change_set.created_files == ["tests/test_agent_hub_demo.py"]

    test_artifact_id = by_type[NodeType.TEST].output_artifact_id
    assert test_artifact_id is not None
    metadata, content = await application.services.artifacts.get_and_verify(
        test_artifact_id,
        expected_session_id=session.session_id,
        expected_task_id=record.change_set.task_id,
    )
    report = json.loads(content)
    assert metadata.artifact_type == "test_result"
    assert report["passed"] is True
    assert report["runner"]["exit_code"] == 0
    assert report["runner"]["timed_out"] is False


def _command_write_graph() -> AuthorGraph:
    return AuthorGraph(
        nodes=[
            WorkflowNode(id="input", node_type=NodeType.INPUT, title="Input"),
            WorkflowNode(
                id="write-test",
                node_type=NodeType.AGENT_TASK,
                task_kind=TaskKind.TEST_FIX,
                title="Create a passing test",
                instruction="Create the exact test file and run the sealed test command.",
                assigned_agent="mock",
                assignment_mode=AssignmentMode.LOCKED,
                new_files_candidate=["tests/test_agent_hub_demo.py"],
                allowed_commands_candidate=[["pytest", "-q", "tests/test_agent_hub_demo.py"]],
                risk_level_hint=RiskLevel.L1,
                requires_write=True,
            ),
            WorkflowNode(id="output", node_type=NodeType.OUTPUT, title="Output"),
        ],
        edges=[
            WorkflowEdge(id="edge-input-write", from_node="input", to_node="write-test"),
            WorkflowEdge(id="edge-write-output", from_node="write-test", to_node="output"),
        ],
    )
