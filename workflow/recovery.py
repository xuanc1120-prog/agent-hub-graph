"""Cancellation linearization and crash recovery for write runs."""

from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path

from protocol import ActorType, ApprovalStatus, ChangeSetStatus, NodeRunStatus, NodeType
from storage.approval_repository import ApprovalRepository
from storage.errors import RecordNotFound
from storage.event_repository import EventRepository
from storage.leases import MasterLease
from storage.workflow_run_repository import WorkflowRunRecord, WorkflowRunRepository
from workflow.capability_broker import CapabilityBroker
from workflow.events import RECOVERY_ACTION, RecoveryEventPayload
from workspace.git_manager import GitManager
from workspace.lock_manager import LockManager, WorkspaceOwnerKind


class CancellationManager:
    def __init__(
        self,
        runs: WorkflowRunRepository,
        approvals: ApprovalRepository,
        capabilities: CapabilityBroker,
        events: EventRepository,
    ) -> None:
        self._runs = runs
        self._approvals = approvals
        self._capabilities = capabilities
        self._events = events

    async def cancel(
        self,
        workflow_run_id: str,
        *,
        lease: MasterLease,
    ) -> WorkflowRunRecord:
        run = await self._runs.request_cancel(workflow_run_id, lease=lease)
        if run.status.value in {
            "completed",
            "failed",
            "blocked",
            "cancelled",
            "orphaned",
        }:
            return run
        await self._approvals.invalidate_for_run(
            workflow_run_id=workflow_run_id,
            master_lease=lease,
            reason="workflow_cancelled",
        )
        await self._capabilities.revoke_for_run(
            workflow_run_id,
            master_lease=lease,
            reason="workflow_cancelled",
        )
        result = await self._runs.complete_cancel(workflow_run_id, lease=lease)
        await self._emit(
            result,
            lease=lease,
            action="cancel",
            outcome="cancelled",
            reason="cancel_requested_at was linearized before completion",
        )
        return result

    async def _emit(
        self,
        run: WorkflowRunRecord,
        *,
        lease: MasterLease,
        action: str,
        outcome: str,
        reason: str,
    ) -> None:
        payload = RecoveryEventPayload(
            master_fencing_token=lease.fencing_token,
            workflow_run_id=run.workflow_run_id,
            session_id=run.session_id,
            action=action,
            outcome=outcome,
            reason=reason,
        )
        await self._events.append(
            session_id=run.session_id,
            workflow_id=run.workflow_id,
            workflow_run_id=run.workflow_run_id,
            event_type=RECOVERY_ACTION,
            actor_type=ActorType.MASTER,
            actor_id=lease.instance_id,
            payload=payload,
        )
        await self._events.append_security_event(
            session_id=run.session_id,
            workflow_run_id=run.workflow_run_id,
            task_id=None,
            event_type="workflow.recovery_action",
            severity="high" if outcome == "orphaned" else "info",
            payload=payload,
        )


class RecoveryManager:
    def __init__(
        self,
        runs: WorkflowRunRepository,
        sessions: object,
        approvals: ApprovalRepository,
        change_sets: object,
        events: EventRepository,
        git: GitManager,
        locks: LockManager,
    ) -> None:
        self._runs = runs
        self._sessions = sessions
        self._approvals = approvals
        self._change_sets = change_sets
        self._events = events
        self._git = git
        self._locks = locks

    async def recover(self, *, lease: MasterLease) -> list[WorkflowRunRecord]:
        recovered: list[WorkflowRunRecord] = []
        for run in await self._runs.list_recoverable():
            if run.merge_finalizing_at is not None:
                recovered.append(await self._recover_finalizing(run, lease=lease))
            elif run.status.value == "running":
                result = await self._runs.mark_orphaned(
                    run.workflow_run_id,
                    lease=lease,
                    reason="startup_found_running_run_without_owner",
                )
                await self._emit(
                    result,
                    lease=lease,
                    action="quarantine_running",
                    outcome="orphaned",
                    reason="startup found an unowned running workflow",
                )
                recovered.append(result)
            else:
                recovered.append(run)
        return recovered

    async def _recover_finalizing(
        self,
        run: WorkflowRunRecord,
        *,
        lease: MasterLease,
    ) -> WorkflowRunRecord:
        try:
            session = await self._sessions.get(run.session_id)
            nodes = await self._runs.list_nodes(run.workflow_run_id)
        except RecordNotFound:
            return await self._orphan(run, lease=lease, reason="recovery lineage is missing")
        merge_node = next(
            (
                node
                for node in nodes
                if node.node_type == NodeType.MERGE_PATCH and node.status == NodeRunStatus.RUNNING
            ),
            None,
        )
        if merge_node is None:
            return await self._orphan(
                run,
                lease=lease,
                reason="merge_finalizing_without_running_merge_node",
            )
        source_id = next(
            (
                node.source_node_id
                for node in run.compiled_snapshot.nodes
                if node.id == merge_node.node_id
            ),
            None,
        )
        if source_id is None:
            return await self._orphan(run, lease=lease, reason="merge_source_missing")
        try:
            record = await self._approvals.get_for_change_set(
                await self._find_change_set_id(
                    run.workflow_run_id,
                    source_id,
                )
            )
        except RecordNotFound:
            return await self._orphan(run, lease=lease, reason="recovery ChangeSet is missing")
        if (
            record is None
            or record.approval.status != ApprovalStatus.APPROVED
            or record.change_set.status != ChangeSetStatus.APPROVED
            or record.approval.base_commit != record.change_set.base_commit
            or record.approval.patch_sha256 != record.change_set.patch_sha256
            or record.approval.evidence_sha256 != _evidence_hash(record)
        ):
            return await self._orphan(run, lease=lease, reason="merge_approval_missing_or_invalid")
        change_set_id = record.approval.change_set_id
        repo = Path(session.shared_repo_path)
        try:
            state = self._git.state(repo)
        except Exception:
            return await self._orphan(run, lease=lease, reason="repository_state_unreadable")
        if state.commit == run.current_commit and not state.dirty:
            result = await self._runs.clear_merge_finalizing(
                run.workflow_run_id,
                lease=lease,
            )
            await self._emit(
                result,
                lease=lease,
                action="clear_uncommitted_finalization",
                outcome="retryable",
                reason="crash occurred before a Master commit was published",
            )
            return result
        try:
            metadata = self._git.read_commit_metadata(repo, state.commit)
            tree = self._git.commit_tree(repo, state.commit)
            parent = self._git.commit_parent(repo, state.commit)
        except Exception:
            return await self._orphan(
                run,
                lease=lease,
                reason="commit evidence is unreadable",
            )
        expected = {
            "Agent-Hub-Session": run.session_id,
            "Agent-Hub-Workflow-Run": run.workflow_run_id,
            "Agent-Hub-Change-Set": change_set_id,
            "Agent-Hub-Approval": record.approval.approval_id,
            "Agent-Hub-Tree": tree,
        }
        parent_ok = parent == run.current_commit
        if (
            not state.dirty
            and parent_ok
            and all(metadata.get(key) == value for key, value in expected.items())
        ):
            async with self._locks.hold(
                session_id=run.session_id,
                owner_kind=WorkspaceOwnerKind.RECOVERY,
                owner_operation_id=f"recovery-{run.workflow_run_id}",
                owner_process_id=os.getpid(),
                ttl_seconds=30,
                heartbeat_seconds=5,
            ) as held:
                await self._runs.finalize_merge(
                    merge_node.node_run_id,
                    change_set_id=change_set_id,
                    new_commit=state.commit,
                    workspace_lease=held.lease,
                    lease=lease,
                    summary="Recovery finalized a previously committed Master merge.",
                )
            result = await self._runs.get(run.workflow_run_id)
            await self._emit(
                result,
                lease=lease,
                action="finalize_committed_merge",
                outcome="recovered",
                reason="commit trailer, parent, tree, and clean repository matched",
            )
            return result
        return await self._orphan(
            run,
            lease=lease,
            reason="merge finalization evidence was ambiguous",
        )

    async def _find_change_set_id(self, workflow_run_id: str, source_node_id: str) -> str:
        record = await self._change_sets.get_for_source_node(
            workflow_run_id=workflow_run_id,
            source_node_id=source_node_id,
        )
        return record.change_set.change_set_id

    async def _orphan(
        self,
        run: WorkflowRunRecord,
        *,
        lease: MasterLease,
        reason: str,
    ) -> WorkflowRunRecord:
        result = await self._runs.mark_orphaned(
            run.workflow_run_id,
            lease=lease,
            reason=reason,
        )
        await self._emit(
            result,
            lease=lease,
            action="quarantine_ambiguous_merge",
            outcome="orphaned",
            reason=reason,
        )
        return result

    async def _emit(
        self,
        run: WorkflowRunRecord,
        *,
        lease: MasterLease,
        action: str,
        outcome: str,
        reason: str,
    ) -> None:
        payload = RecoveryEventPayload(
            master_fencing_token=lease.fencing_token,
            workflow_run_id=run.workflow_run_id,
            session_id=run.session_id,
            action=action,
            outcome=outcome,
            reason=reason,
        )
        await self._events.append(
            session_id=run.session_id,
            workflow_id=run.workflow_id,
            workflow_run_id=run.workflow_run_id,
            event_type=RECOVERY_ACTION,
            actor_type=ActorType.MASTER,
            actor_id=lease.instance_id,
            payload=payload,
        )
        await self._events.append_security_event(
            session_id=run.session_id,
            workflow_run_id=run.workflow_run_id,
            task_id=None,
            event_type="workflow.recovery_action",
            severity="info",
            payload=payload,
        )


__all__ = ["CancellationManager", "RecoveryManager"]


def _evidence_hash(record: object) -> str:
    refs = record.change_set.evidence_refs
    value = sorted((ref.artifact_id, ref.sha256, ref.artifact_type.value) for ref in refs)
    return sha256(json.dumps(value, separators=(",", ":")).encode("utf-8")).hexdigest()
