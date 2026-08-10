"""Cancellation linearization and crash recovery for write runs."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from protocol import ActorType, ApprovalStatus, ChangeSetStatus, NodeRunStatus, NodeType
from storage.approval_repository import ApprovalRepository
from storage.artifact_repository import ArtifactRepository
from storage.errors import RecordNotFound
from storage.event_repository import EventRepository
from storage.leases import MasterLease
from storage.workflow_run_repository import WorkflowRunRecord, WorkflowRunRepository
from workflow.approval_evidence import ApprovalEvidenceError, build_manifest, manifest_hash
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
        return await self._runs.cancel_with_cleanup(
            workflow_run_id,
            approvals=self._approvals,
            capabilities=self._capabilities,
            lease=lease,
            reason="workflow_cancelled",
        )

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
        artifacts: ArtifactRepository | None = None,
        capabilities: CapabilityBroker | None = None,
    ) -> None:
        self._runs = runs
        self._sessions = sessions
        self._approvals = approvals
        self._change_sets = change_sets
        self._events = events
        self._git = git
        self._locks = locks
        self._artifacts = artifacts
        self._capabilities = capabilities

    async def recover(self, *, lease: MasterLease) -> list[WorkflowRunRecord]:
        recovered: list[WorkflowRunRecord] = []
        for run in await self._runs.list_recoverable():
            if run.cancel_requested_at is not None and run.merge_finalizing_at is None:
                if self._capabilities is None:
                    recovered.append(
                        await self._orphan(
                            run,
                            lease=lease,
                            reason="partial cancellation lacks capability cleanup runtime",
                        )
                    )
                    continue
                try:
                    result = await self._runs.cancel_with_cleanup(
                        run.workflow_run_id,
                        approvals=self._approvals,
                        capabilities=self._capabilities,
                        lease=lease,
                        reason="recovery_completed_partial_cancellation",
                    )
                except Exception:
                    recovered.append(
                        await self._orphan(
                            run,
                            lease=lease,
                            reason="partial cancellation recovery failed",
                        )
                    )
                else:
                    await self._emit(
                        result,
                        lease=lease,
                        action="complete_partial_cancellation",
                        outcome="recovered",
                        reason="startup completed the durable cancellation request",
                    )
                    recovered.append(result)
            elif run.merge_finalizing_at is not None:
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
        orphan_reason: str | None = None
        result: WorkflowRunRecord | None = None
        action: str | None = None
        outcome: str | None = None
        reason: str | None = None
        try:
            async with self._locks.hold(
                session_id=run.session_id,
                owner_kind=WorkspaceOwnerKind.RECOVERY,
                owner_operation_id=f"recovery-{run.workflow_run_id}",
                owner_process_id=os.getpid(),
                ttl_seconds=30,
                heartbeat_seconds=5,
            ) as held:
                held.assert_healthy()
                if run.cancel_requested_at is not None:
                    orphan_reason = "cancellation raced with merge finalization"
                else:
                    try:
                        session = await self._sessions.get(run.session_id)
                        nodes = await self._runs.list_nodes(run.workflow_run_id)
                    except RecordNotFound:
                        orphan_reason = "recovery lineage is missing"
                    if orphan_reason is None:
                        merge_node = next(
                            (
                                node
                                for node in nodes
                                if node.node_type == NodeType.MERGE_PATCH
                                and node.status == NodeRunStatus.RUNNING
                            ),
                            None,
                        )
                        if merge_node is None:
                            orphan_reason = "merge_finalizing_without_running_merge_node"
                        else:
                            source_id = next(
                                (
                                    node.source_node_id
                                    for node in run.compiled_snapshot.nodes
                                    if node.id == merge_node.node_id
                                ),
                                None,
                            )
                            if source_id is None:
                                orphan_reason = "merge_source_missing"
                            else:
                                try:
                                    change_set = await self._change_sets.get_for_source_node(
                                        workflow_run_id=run.workflow_run_id,
                                        source_node_id=source_id,
                                    )
                                    approval = await self._approvals.get_for_change_set(
                                        change_set.change_set.change_set_id
                                    )
                                except RecordNotFound:
                                    orphan_reason = "recovery ChangeSet is missing"
                                if orphan_reason is None:
                                    try:
                                        evidence_hash = await self._current_evidence_hash(
                                            run, change_set, nodes
                                        )
                                    except ApprovalEvidenceError:
                                        evidence_hash = ""
                                    if (
                                        approval is None
                                        or approval.approval.status != ApprovalStatus.APPROVED
                                        or approval.approval.expires_at <= datetime.now(UTC)
                                        or change_set.change_set.status != ChangeSetStatus.APPROVED
                                        or approval.approval.base_commit
                                        != change_set.change_set.base_commit
                                        or approval.approval.patch_sha256
                                        != change_set.change_set.patch_sha256
                                        or approval.approval.evidence_sha256 != evidence_hash
                                    ):
                                        orphan_reason = "merge_approval_missing_or_invalid"
                                if orphan_reason is None:
                                    change_set_id = change_set.change_set.change_set_id
                                    repo = Path(session.shared_repo_path)
                                    state = self._git.state(repo)
                                    held.assert_healthy()
                                    if state.commit == run.current_commit and not state.dirty:
                                        result = await self._runs.clear_merge_finalizing(
                                            run.workflow_run_id,
                                            workspace_lease=held.lease,
                                            lease=lease,
                                        )
                                        action = "clear_uncommitted_finalization"
                                        outcome = "retryable"
                                        reason = (
                                            "crash occurred before a Master commit was published"
                                        )
                                    else:
                                        _stored, patch = await self._change_sets.load_patch(
                                            change_set_id
                                        )
                                        expected_tree = self._git.canonical_patch_tree(
                                            repo,
                                            parent_commit=change_set.change_set.base_commit,
                                            patch_bytes=patch,
                                        )
                                        metadata = self._git.read_commit_metadata(
                                            repo, state.commit
                                        )
                                        tree = self._git.commit_tree(repo, state.commit)
                                        parent = self._git.commit_parent(repo, state.commit)
                                        expected = {
                                            "Agent-Hub-Session": run.session_id,
                                            "Agent-Hub-Workflow-Run": run.workflow_run_id,
                                            "Agent-Hub-Change-Set": change_set_id,
                                            "Agent-Hub-Approval": approval.approval.approval_id,
                                            "Agent-Hub-Patch": change_set.change_set.patch_sha256,
                                            "Agent-Hub-Post-State": (
                                                change_set.change_set.post_state_hash
                                            ),
                                            "Agent-Hub-Tree": tree,
                                        }
                                        parent_ok = (
                                            parent
                                            == run.current_commit
                                            == change_set.change_set.base_commit
                                        )
                                        if (
                                            not state.dirty
                                            and parent_ok
                                            and tree == expected_tree
                                            and all(
                                                metadata.get(key) == value
                                                for key, value in expected.items()
                                            )
                                        ):
                                            await self._runs.finalize_merge(
                                                merge_node.node_run_id,
                                                change_set_id=change_set_id,
                                                new_commit=state.commit,
                                                workspace_lease=held.lease,
                                                lease=lease,
                                                summary=(
                                                    "Recovery finalized a previously committed "
                                                    "Master merge."
                                                ),
                                            )
                                            result = await self._runs.get(run.workflow_run_id)
                                            action = "finalize_committed_merge"
                                            outcome = "recovered"
                                            reason = (
                                                "commit trailer, canonical patch tree, parent, "
                                                "and clean repository matched"
                                            )
                                        else:
                                            orphan_reason = (
                                                "merge finalization evidence was ambiguous"
                                            )
        except Exception:
            orphan_reason = "merge finalization evidence was unreadable or ambiguous"
        if result is not None:
            assert action is not None and outcome is not None and reason is not None
            await self._emit(
                result,
                lease=lease,
                action=action,
                outcome=outcome,
                reason=reason,
            )
            return result
        return await self._orphan(
            run,
            lease=lease,
            reason=orphan_reason or "merge finalization evidence was ambiguous",
        )

    async def _current_evidence_hash(
        self,
        run: WorkflowRunRecord,
        change_set: object,
        nodes: list[object],
    ) -> str:
        if self._artifacts is None:
            return _evidence_hash(change_set)
        task = await self._runs.get_task(change_set.change_set.task_id)
        manifest = await build_manifest(
            run=run,
            change_set=change_set,
            nodes=nodes,
            artifacts=self._artifacts,
            runtime_policy_artifact_id=task.runtime_policy_artifact_id,
            capability_lineage=await self._approvals.list_privilege_lineage_for_run(
                run.workflow_run_id
            ),
        )
        return manifest_hash(manifest)

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
