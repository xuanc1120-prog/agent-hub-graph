from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import Settings
from app.services import WorkflowApplication
from protocol import (
    ApprovalStatus,
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
from storage.errors import ConcurrencyConflict
from storage.repositories import NewWorkflow


async def _waiting_application(
    fixture_source_repo: Path,
    tmp_path: Path,
    *,
    suffix: str,
) -> tuple[WorkflowApplication, object, object]:
    application = WorkflowApplication(Settings(data_dir=tmp_path / "agent-hub-data"))
    await application.initialize()
    await application.register_mock_agent()
    session = await application.create_session(
        repo=fixture_source_repo,
        goal="Create and run one sealed passing test.",
        session_id=f"session-hub210-{suffix}",
    )
    workflow = await application.services.workflows.create(
        NewWorkflow(
            workflow_id=f"workflow-hub210-{suffix}",
            session_id=session.session_id,
            author_graph=_command_write_graph(),
            layout=WorkflowLayout(),
        )
    )
    async with application.temporary_master() as lease:
        run = await application.run(
            workflow.workflow_id,
            lease=lease,
            workflow_run_id=f"run-hub210-{suffix}",
        )
    assert run.status == WorkflowRunStatus.WAITING_APPROVAL
    return application, session, run


@pytest.mark.asyncio
async def test_approval_cas_allows_only_one_concurrent_decision(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    application, _session, run = await _waiting_application(
        fixture_source_repo,
        tmp_path,
        suffix="cas",
    )
    approval = await application.services.approvals.get_for_change_set(
        (
            await application.services.change_sets.get_for_source_node(
                workflow_run_id=run.workflow_run_id,
                source_node_id="write-test",
            )
        ).change_set.change_set_id
    )
    assert approval is not None

    async with application.temporary_master() as lease:

        async def decide(approved: bool) -> str:
            try:
                await application.services.approval_manager.decide(
                    approval.approval.approval_id,
                    expected_version=approval.approval.version,
                    confirm_subject_hash=approval.approval.subject_sha256,
                    approved=approved,
                    actor_id="reviewer",
                    idempotency_key=f"decision-{approved}",
                    master_lease=lease,
                )
            except ConcurrencyConflict:
                return "lost"
            return "won"

        results = await asyncio.gather(decide(True), decide(False))

    assert sorted(results) == ["lost", "won"]
    current = await application.services.approvals.get(approval.approval.approval_id)
    assert current.approval.status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}
    assert current.approval.version == 2


@pytest.mark.asyncio
async def test_cancel_invalidates_waiting_approval_and_changeset(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    application, _session, run = await _waiting_application(
        fixture_source_repo,
        tmp_path,
        suffix="cancel",
    )
    change_set = await application.services.change_sets.get_for_source_node(
        workflow_run_id=run.workflow_run_id,
        source_node_id="write-test",
    )
    approval = await application.services.approvals.get_for_change_set(
        change_set.change_set.change_set_id
    )
    assert approval is not None

    async with application.temporary_master() as lease:
        cancelled = await application.services.cancellation.cancel(
            run.workflow_run_id,
            lease=lease,
        )

    assert cancelled.status == WorkflowRunStatus.CANCELLED
    current = await application.services.approvals.get(approval.approval.approval_id)
    assert current.approval.status == ApprovalStatus.INVALIDATED
    refreshed = await application.services.change_sets.get(change_set.change_set.change_set_id)
    assert refreshed.change_set.status == ChangeSetStatus.CANCELLED
    assert not application.services.git.state(_session.shared_repo_path).dirty
    nodes = await application.services.runs.list_nodes(run.workflow_run_id)
    assert all(node.status in {NodeRunStatus.CANCELLED, NodeRunStatus.COMPLETED} for node in nodes)


@pytest.mark.asyncio
async def test_approved_changeset_is_merged_by_master_scheduler(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    application, session, run = await _waiting_application(
        fixture_source_repo,
        tmp_path,
        suffix="merge",
    )
    change_set = await application.services.change_sets.get_for_source_node(
        workflow_run_id=run.workflow_run_id,
        source_node_id="write-test",
    )
    approval = await application.services.approvals.get_for_change_set(
        change_set.change_set.change_set_id
    )
    assert approval is not None

    async with application.temporary_master() as lease:
        await application.services.approval_manager.approve(
            approval.approval.approval_id,
            expected_version=approval.approval.version,
            confirm_subject_hash=approval.approval.subject_sha256,
            actor_id="reviewer",
            idempotency_key="decision-merge",
            master_lease=lease,
        )
        approved = await application.services.approvals.get(approval.approval.approval_id)
        assert approved.approval.evidence_sha256 == approval.approval.evidence_sha256
        completed = await application.services.scheduler.run_until_stable(
            run.workflow_run_id,
            lease=lease,
        )

    assert completed.status == WorkflowRunStatus.COMPLETED
    assert completed.current_commit != run.current_commit
    merged = await application.services.change_sets.get(change_set.change_set.change_set_id)
    assert merged.change_set.status == ChangeSetStatus.MERGED
    assert application.services.git.state(session.shared_repo_path).dirty is False
    assert (session.shared_repo_path / "tests" / "test_agent_hub_demo.py").exists()


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
