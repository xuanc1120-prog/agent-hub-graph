from __future__ import annotations

import json
import subprocess
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
async def test_passing_test_with_workspace_side_effect_is_rejected_and_restored(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    _commit_side_effect_test(fixture_source_repo)
    application = WorkflowApplication(Settings(data_dir=tmp_path / "agent-hub-data"))
    await application.initialize()
    await application.register_mock_agent()
    source_state = application.services.git.state(fixture_source_repo)
    assert not source_state.dirty, source_state
    session = await application.create_session(
        repo=fixture_source_repo,
        goal="Reject test commands that mutate the shared workspace.",
        session_id="session-hub200-side-effect",
    )
    workflow = await application.services.workflows.create(
        NewWorkflow(
            workflow_id="workflow-hub200-side-effect",
            session_id=session.session_id,
            author_graph=_side_effect_graph(),
            layout=WorkflowLayout(),
        )
    )

    async with application.temporary_master() as lease:
        run = await application.run(
            workflow.workflow_id,
            lease=lease,
            workflow_run_id="run-hub200-side-effect",
        )

    assert run.status == WorkflowRunStatus.FAILED
    state = application.services.git.state(session.shared_repo_path)
    assert state.dirty is False
    assert not (session.shared_repo_path / "src" / "agent_hub_demo.py").exists()
    assert not (session.shared_repo_path / "unexpected-test-output.txt").exists()

    node_runs = await application.services.runs.list_nodes(run.workflow_run_id)
    test_node = next(node for node in node_runs if node.node_type == NodeType.TEST)
    assert test_node.status == NodeRunStatus.FAILED
    assert test_node.error_code == "test_failed"

    record = await application.services.change_sets.get_for_source_node(
        workflow_run_id=run.workflow_run_id,
        source_node_id="write-source",
    )
    assert record.change_set.status == ChangeSetStatus.TEST_FAILED

    assert test_node.output_artifact_id is not None
    _metadata, content = await application.services.artifacts.get_and_verify(
        test_node.output_artifact_id,
        expected_session_id=session.session_id,
        expected_task_id=record.change_set.task_id,
    )
    report = json.loads(content)
    assert report["runner"]["exit_code"] == 0
    assert report["reasons"] == ["test_mutated_workspace"]


def _commit_side_effect_test(repo: Path) -> None:
    test_path = repo / "tests" / "test_side_effect.py"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(
        "from pathlib import Path\n\n"
        "def test_side_effect():\n"
        "    Path('unexpected-test-output.txt').write_text('unexpected', encoding='utf-8')\n",
        encoding="utf-8",
        newline="\n",
    )
    subprocess.run(
        ["git", "add", "tests/test_side_effect.py"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Agent Hub Tests",
            "-c",
            "user.email=tests@agent-hub.local",
            "commit",
            "-m",
            "add side-effect fixture",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _side_effect_graph() -> AuthorGraph:
    return AuthorGraph(
        nodes=[
            WorkflowNode(id="input", node_type=NodeType.INPUT, title="Input"),
            WorkflowNode(
                id="write-source",
                node_type=NodeType.AGENT_TASK,
                task_kind=TaskKind.IMPLEMENT,
                title="Create a source file",
                instruction="Create the exact source file and run the sealed test command.",
                assigned_agent="mock",
                assignment_mode=AssignmentMode.LOCKED,
                new_files_candidate=["src/agent_hub_demo.py"],
                allowed_commands_candidate=[["pytest", "-q", "tests/test_side_effect.py"]],
                risk_level_hint=RiskLevel.L1,
                requires_write=True,
            ),
            WorkflowNode(id="output", node_type=NodeType.OUTPUT, title="Output"),
        ],
        edges=[
            WorkflowEdge(id="edge-input-write", from_node="input", to_node="write-source"),
            WorkflowEdge(id="edge-write-output", from_node="write-source", to_node="output"),
        ],
    )
