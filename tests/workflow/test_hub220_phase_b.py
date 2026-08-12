"""HUB-220 Phase B fault-injection tests for the HUB-210 runtime boundary."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import Settings
from app.services import WorkflowApplication
from protocol import (
    ApprovalStatus,
    AssignmentMode,
    AuthorGraph,
    ChangeSetStatus,
    NodeType,
    RiskLevel,
    TaskKind,
    WorkflowEdge,
    WorkflowLayout,
    WorkflowNode,
    WorkflowRunStatus,
)
from storage.errors import (
    ChangeSetReconciliationRequired,
    ConcurrencyConflict,
    IdempotencyConflict,
)
from storage.repositories import NewWorkflow
from workflow.events import APPROVAL_STATE_CHANGED
from workspace.lock_manager import WorkspaceOwnerKind


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
        goal="Exercise the HUB-220 Phase B write boundary.",
        session_id=f"session-hub220-{suffix}",
    )
    workflow = await application.services.workflows.create(
        NewWorkflow(
            workflow_id=f"workflow-hub220-{suffix}",
            session_id=session.session_id,
            author_graph=_command_write_graph(),
            layout=WorkflowLayout(),
        )
    )
    async with application.temporary_master() as lease:
        run = await application.run(
            workflow.workflow_id,
            lease=lease,
            workflow_run_id=f"run-hub220-{suffix}",
        )
    assert run.status == WorkflowRunStatus.WAITING_APPROVAL
    return application, session, run


async def _change_set_and_approval(
    application: WorkflowApplication, run: object
) -> tuple[object, object]:
    change_set = await application.services.change_sets.get_for_source_node(
        workflow_run_id=run.workflow_run_id,
        source_node_id="write-test",
    )
    approval = await application.services.approvals.get_for_change_set(
        change_set.change_set.change_set_id
    )
    assert approval is not None
    return change_set, approval


async def _approve(application: WorkflowApplication, approval: object, lease: object) -> None:
    await application.services.approval_manager.approve(
        approval.approval.approval_id,
        expected_version=approval.approval.version,
        confirm_subject_hash=approval.approval.subject_sha256,
        actor_id="phase-b-reviewer",
        idempotency_key=f"phase-b-approve-{approval.approval.approval_id}",
        master_lease=lease,
    )


@pytest.mark.asyncio
async def test_phase_b_approval_cas_has_one_linearized_winner(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    application, _session, run = await _waiting_application(
        fixture_source_repo, tmp_path, suffix="approval-cas"
    )
    _change_set, approval = await _change_set_and_approval(application, run)

    async with application.temporary_master() as lease:

        async def decide(approved: bool) -> str:
            try:
                await application.services.approval_manager.decide(
                    approval.approval.approval_id,
                    expected_version=approval.approval.version,
                    confirm_subject_hash=approval.approval.subject_sha256,
                    approved=approved,
                    actor_id="phase-b-reviewer",
                    idempotency_key=f"phase-b-decision-{approved}",
                    master_lease=lease,
                )
            except ConcurrencyConflict:
                return "lost"
            return "won"

        results = await asyncio.gather(decide(True), decide(False))
        assert sorted(results) == ["lost", "won"]
        current = await application.services.approvals.get(approval.approval.approval_id)
        assert current.approval.status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}
        assert current.approval.version == approval.approval.version + 1
        winning_key = f"phase-b-decision-{current.approval.status == ApprovalStatus.APPROVED}"

        # Simulate a v3 decided row upgraded by v4: the request hash was not
        # persisted yet, but the immutable decision fields remain available.
        async with application.services.database.connection() as connection:
            await connection.execute(
                "UPDATE approvals SET decision_request_sha256 = NULL WHERE id = ?",
                (approval.approval.approval_id,),
            )
        replayed = await application.services.approval_manager.decide(
            approval.approval.approval_id,
            expected_version=approval.approval.version,
            confirm_subject_hash=current.approval.subject_sha256,
            approved=current.approval.status == ApprovalStatus.APPROVED,
            actor_id="phase-b-reviewer",
            idempotency_key=winning_key,
            master_lease=lease,
        )
        assert replayed.approval.version == current.approval.version
        assert replayed.decision_request_sha256 is not None
        with pytest.raises(IdempotencyConflict, match="different request"):
            await application.services.approval_manager.decide(
                approval.approval.approval_id,
                expected_version=current.approval.version,
                confirm_subject_hash=current.approval.subject_sha256,
                approved=current.approval.status == ApprovalStatus.APPROVED,
                actor_id="phase-b-reviewer",
                idempotency_key=winning_key,
                master_lease=lease,
            )
        with pytest.raises(IdempotencyConflict, match="different request"):
            await application.services.approval_manager.decide(
                approval.approval.approval_id,
                expected_version=approval.approval.version,
                confirm_subject_hash=current.approval.subject_sha256,
                approved=current.approval.status == ApprovalStatus.APPROVED,
                actor_id="different-reviewer",
                idempotency_key=winning_key,
                master_lease=lease,
            )
        with pytest.raises(IdempotencyConflict, match="different request"):
            await application.services.approval_manager.decide(
                approval.approval.approval_id,
                expected_version=approval.approval.version,
                confirm_subject_hash=current.approval.subject_sha256,
                approved=current.approval.status != ApprovalStatus.APPROVED,
                actor_id="phase-b-reviewer",
                idempotency_key=winning_key,
                master_lease=lease,
            )

        events = await application.services.events.list_all_by_run(
            run.workflow_run_id,
            page_size=500,
        )
        approval_events = [
            event
            for event in events
            if event.event_type == APPROVAL_STATE_CHANGED
            and json.loads(event.payload_json)["approval_id"] == approval.approval.approval_id
        ]
        assert len(approval_events) == 2
        assert len({event.run_seq for event in approval_events}) == 2
        assert [json.loads(event.payload_json)["version"] for event in approval_events] == [1, 2]


@pytest.mark.asyncio
async def test_phase_b_merge_finalizing_wins_over_concurrent_cancel(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    application, session, run = await _waiting_application(
        fixture_source_repo, tmp_path, suffix="merge-cancel"
    )
    change_set, approval = await _change_set_and_approval(application, run)

    async with application.temporary_master() as lease:
        await _approve(application, approval, lease)
        entered_load = asyncio.Event()
        release_load = asyncio.Event()
        original_load_patch = application.services.change_sets.load_patch

        async def gated_load_patch(change_set_id: str):
            entered_load.set()
            await release_load.wait()
            return await original_load_patch(change_set_id)

        with patch.object(
            application.services.change_sets,
            "load_patch",
            side_effect=gated_load_patch,
        ):
            merge_task = asyncio.create_task(
                application.services.scheduler.run_until_stable(
                    run.workflow_run_id,
                    lease=lease,
                )
            )
            try:
                await asyncio.wait_for(entered_load.wait(), timeout=10)
                with pytest.raises(ConcurrencyConflict, match="merge finalization"):
                    await application.services.cancellation.cancel(
                        run.workflow_run_id,
                        lease=lease,
                    )
                release_load.set()
                completed = await asyncio.wait_for(merge_task, timeout=20)
            finally:
                release_load.set()
                if not merge_task.done():
                    merge_task.cancel()
                with suppress(asyncio.CancelledError, asyncio.TimeoutError):
                    await asyncio.wait_for(merge_task, timeout=5)

    assert completed.status == WorkflowRunStatus.COMPLETED
    assert completed.cancel_requested_at is None
    assert completed.merge_finalizing_at is None
    merged = await application.services.change_sets.get(change_set.change_set.change_set_id)
    assert merged.change_set.status == ChangeSetStatus.MERGED
    assert application.services.git.state(session.shared_repo_path).dirty is False


@pytest.mark.asyncio
async def test_phase_b_recovery_finalizes_commit_published_before_crash(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    application, session, run = await _waiting_application(
        fixture_source_repo, tmp_path, suffix="recovery-committed"
    )
    change_set, approval = await _change_set_and_approval(application, run)
    published_commit: str

    async with application.temporary_master() as lease:
        await _approve(application, approval, lease)
        original_finalize = application.services.runs.finalize_merge
        fail_once = True

        async def crash_after_commit(*args: object, **kwargs: object):
            nonlocal fail_once
            if fail_once:
                fail_once = False
                raise RuntimeError("simulated Master crash after commit")
            return await original_finalize(*args, **kwargs)

        with (
            patch.object(
                application.services.runs,
                "finalize_merge",
                side_effect=crash_after_commit,
            ),
            pytest.raises(ChangeSetReconciliationRequired),
        ):
            await application.services.scheduler.run_until_stable(
                run.workflow_run_id,
                lease=lease,
            )

        interrupted = await application.services.runs.get(run.workflow_run_id)
        assert interrupted.merge_finalizing_at is not None
        assert interrupted.status == WorkflowRunStatus.RUNNING
        # Durable run state still points at the pre-merge commit until
        # RecoveryManager atomically finalizes the already-published commit.
        assert interrupted.current_commit == run.current_commit
        assert application.services.git.state(session.shared_repo_path).commit != run.current_commit
        assert application.services.git.state(session.shared_repo_path).dirty is False
        published_commit = application.services.git.state(session.shared_repo_path).commit
        assert published_commit is not None

    restarted = WorkflowApplication(Settings(data_dir=tmp_path / "agent-hub-data"))
    await restarted.initialize()
    async with restarted.temporary_master() as recovery_lease:
        recovered = await restarted.services.runs.get(run.workflow_run_id)
        assert recovered.status == WorkflowRunStatus.RUNNING
        assert recovered.merge_finalizing_at is None
        assert recovered.current_commit == published_commit
        assert restarted.services.git.state(session.shared_repo_path).commit == published_commit
        completed = await restarted.services.scheduler.run_until_stable(
            run.workflow_run_id,
            lease=recovery_lease,
        )
        assert completed.status == WorkflowRunStatus.COMPLETED
        assert completed.current_commit == published_commit
        assert restarted.services.git.state(session.shared_repo_path).commit == published_commit

    first_restart_events = await restarted.services.events.list_all_by_run(
        run.workflow_run_id,
        page_size=500,
    )
    first_restart_event_fingerprint = [
        (event.event_type, event.run_seq, event.payload_json) for event in first_restart_events
    ]

    second_restarted = WorkflowApplication(Settings(data_dir=tmp_path / "agent-hub-data"))
    await second_restarted.initialize()
    async with second_restarted.temporary_master():
        second_run = await second_restarted.services.runs.get(run.workflow_run_id)
        assert second_run.status == WorkflowRunStatus.COMPLETED
        assert second_run.current_commit == published_commit
        assert (
            second_restarted.services.git.state(session.shared_repo_path).commit == published_commit
        )

    second_restart_events = await second_restarted.services.events.list_all_by_run(
        run.workflow_run_id,
        page_size=500,
    )
    assert [
        (event.event_type, event.run_seq, event.payload_json) for event in second_restart_events
    ] == first_restart_event_fingerprint

    merged = await restarted.services.change_sets.get(change_set.change_set.change_set_id)
    assert merged.change_set.status == ChangeSetStatus.MERGED


@pytest.mark.asyncio
async def test_phase_b_recovery_clears_finalizing_before_commit(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    application, session, run = await _waiting_application(
        fixture_source_repo, tmp_path, suffix="recovery-uncommitted"
    )
    change_set, approval = await _change_set_and_approval(application, run)

    async with application.temporary_master() as lease:
        await _approve(application, approval, lease)
        await application.services.scheduler.tick(run.workflow_run_id, lease=lease)
        claimed = await application.services.runs.claim_next(run.workflow_run_id, lease=lease)
        assert claimed is not None
        assert claimed.node_type == NodeType.MERGE_PATCH
        merge_node = next(
            node
            for node in await application.services.runs.list_nodes(run.workflow_run_id)
            if node.node_type == NodeType.MERGE_PATCH
        )
        workspace_lease = await application.services.locks.acquire(
            session_id=session.session_id,
            owner_kind=WorkspaceOwnerKind.MERGE,
            owner_operation_id=merge_node.node_run_id,
            owner_process_id=os.getpid(),
            ttl_seconds=30,
        )
        try:
            await application.services.runs.begin_merge_finalizing(
                run.workflow_run_id,
                workspace_lease=workspace_lease,
                lease=lease,
            )
        finally:
            await application.services.locks.release(workspace_lease)

        interrupted = await application.services.runs.get(run.workflow_run_id)
        assert interrupted.merge_finalizing_at is not None
        assert application.services.git.state(session.shared_repo_path).commit == run.current_commit
        assert application.services.git.state(session.shared_repo_path).dirty is False

    restarted = WorkflowApplication(Settings(data_dir=tmp_path / "agent-hub-data"))
    await restarted.initialize()
    async with restarted.temporary_master() as recovery_lease:
        recovered = await restarted.services.runs.get(run.workflow_run_id)
        assert recovered.status == WorkflowRunStatus.RUNNING
        assert recovered.merge_finalizing_at is None
        # Recovery deliberately stops here: the clean uncommitted state is
        # safe, but the running attempt waits for an explicit retry/cancel
        # decision instead of silently replaying a write.
        assert restarted.services.git.state(session.shared_repo_path).dirty is False

        cancelled = await restarted.services.cancellation.cancel(
            run.workflow_run_id,
            lease=recovery_lease,
        )
        assert cancelled.status == WorkflowRunStatus.CANCELLED

    final_run = await restarted.services.runs.get(run.workflow_run_id)
    assert final_run.status == WorkflowRunStatus.CANCELLED
    merged = await restarted.services.change_sets.get(change_set.change_set.change_set_id)
    assert merged.change_set.status == ChangeSetStatus.CANCELLED
    approval_state = await restarted.services.approvals.get(approval.approval.approval_id)
    assert approval_state.approval.status == ApprovalStatus.APPROVED
    assert approval_state.approval.version == approval.approval.version + 1
    assert restarted.services.git.state(session.shared_repo_path).dirty is False


@pytest.mark.asyncio
async def test_phase_b_recovery_orphans_ambiguous_finalization(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    application, session, run = await _waiting_application(
        fixture_source_repo, tmp_path, suffix="recovery-ambiguous"
    )
    _change_set, approval = await _change_set_and_approval(application, run)

    async with application.temporary_master() as lease:
        await _approve(application, approval, lease)

        async def leave_finalizing(*args: object, **kwargs: object):
            raise RuntimeError("simulated crash before finalization state commit")

        with (
            patch.object(
                application.services.git,
                "commit_approved_patch",
                side_effect=RuntimeError("simulated commit boundary crash"),
            ),
            patch.object(
                application.services.runs,
                "clear_merge_finalizing",
                side_effect=leave_finalizing,
            ),
            pytest.raises(ChangeSetReconciliationRequired),
        ):
            await application.services.scheduler.run_until_stable(
                run.workflow_run_id,
                lease=lease,
            )

        interrupted = await application.services.runs.get(run.workflow_run_id)
        assert interrupted.merge_finalizing_at is not None
        assert application.services.git.state(session.shared_repo_path).dirty is False

    # A repository mutation after the crash makes the finalization evidence
    # ambiguous. Recovery must quarantine the run instead of guessing.
    marker = session.shared_repo_path / "recovery-ambiguous.txt"
    marker.write_text("untrusted mutation\n", encoding="utf-8")
    restarted = WorkflowApplication(Settings(data_dir=tmp_path / "agent-hub-data"))
    await restarted.initialize()
    async with restarted.temporary_master():
        recovered = await restarted.services.runs.get(run.workflow_run_id)
        assert recovered.status == WorkflowRunStatus.ORPHANED
        assert recovered.merge_finalizing_at is not None
        marker.unlink()
        assert restarted.services.git.state(session.shared_repo_path).dirty is False
