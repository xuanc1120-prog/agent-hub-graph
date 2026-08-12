"""Canonical approved-patch application and Master commit handler."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from protocol import ApprovalStatus, ChangeSetStatus, NodeOutcome, NodeRunStatus
from storage.approval_repository import ApprovalRepository
from storage.artifact_repository import ArtifactRepository
from storage.change_set_repository import ChangeSetRepository
from storage.errors import ChangeSetReconciliationRequired, ConcurrencyConflict
from storage.workflow_run_repository import WorkflowRunRepository
from workflow.approval_evidence import build_manifest, manifest_hash
from workflow.handlers.base import NodeExecutionContext, NodeHandlerResult
from workspace.git_manager import GitManager, GitManagerError
from workspace.lock_manager import HeldWorkspaceLease, LockManager, WorkspaceOwnerKind


def _evidence_hash(record: object) -> str:
    refs = record.change_set.evidence_refs
    value = sorted((ref.artifact_id, ref.sha256, ref.artifact_type.value) for ref in refs)
    return sha256(json.dumps(value, separators=(",", ":")).encode("utf-8")).hexdigest()


def _paths(record: object) -> tuple[str, ...]:
    change_set = record.change_set
    values = list(change_set.created_files) + list(change_set.modified_files)
    values += list(change_set.deleted_files) + list(change_set.renamed_files)
    values += list(change_set.untracked_files)
    values += [
        path
        for change in record.document.manifest.changes
        for path in (change.old_path,)
        if path is not None
    ]
    return tuple(sorted(set(values)))


class MergePatchNodeHandler:
    def __init__(
        self,
        runs: WorkflowRunRepository,
        change_sets: ChangeSetRepository,
        approvals: ApprovalRepository,
        git: GitManager,
        locks: LockManager,
        *,
        artifacts: ArtifactRepository | None = None,
        workspace_lease_ttl_seconds: int = 30,
        workspace_heartbeat_seconds: float = 5,
    ) -> None:
        self._runs = runs
        self._change_sets = change_sets
        self._approvals = approvals
        self._git = git
        self._locks = locks
        self._artifacts = artifacts
        self._ttl = workspace_lease_ttl_seconds
        self._heartbeat = workspace_heartbeat_seconds

    async def execute(self, value: object) -> NodeHandlerResult:
        if not isinstance(value, NodeExecutionContext):
            raise TypeError("merge handler requires NodeExecutionContext")
        context = value
        source_id = context.node.source_node_id
        if source_id is None:
            raise ValueError("merge node has no source node")
        record = await self._change_sets.get_for_source_node(
            workflow_run_id=context.run.workflow_run_id,
            source_node_id=source_id,
        )
        approval = await self._approvals.get_for_change_set(record.change_set.change_set_id)
        if approval is None or approval.approval.status != ApprovalStatus.APPROVED:
            raise ConcurrencyConflict("MergePatch requires an approved ChangeSet approval")
        if approval.approval.expires_at <= datetime.now(UTC):
            raise ConcurrencyConflict("MergePatch approval has expired")
        if record.change_set.status != ChangeSetStatus.APPROVED:
            raise ConcurrencyConflict("MergePatch requires an approved ChangeSet")
        if approval.approval.base_commit != record.change_set.base_commit:
            raise ConcurrencyConflict("approval base commit drifted")
        if approval.approval.patch_sha256 != record.change_set.patch_sha256:
            raise ConcurrencyConflict("approval patch hash drifted")
        source = next(
            (node for node in context.run.compiled_snapshot.nodes if node.id == source_id),
            None,
        )
        if source is None:
            raise ValueError("merge source node is absent from snapshot")
        expected_scope = sorted(
            set((source.effective_allowed_files or []) + (source.effective_new_files or []))
        )
        if sorted(approval.approval.scope) != expected_scope:
            raise ConcurrencyConflict("approval scope drifted")
        changed_paths = _paths(record)
        if not changed_paths:
            raise GitManagerError("MergePatch ChangeSet has no changed paths")
        applied = False
        finalizing = False
        committed: str | None = None
        expected_index_sha256: str | None = None
        async with self._locks.hold(
            session_id=context.run.session_id,
            owner_kind=WorkspaceOwnerKind.MERGE,
            owner_operation_id=context.node_run.node_run_id,
            owner_process_id=os.getpid(),
            ttl_seconds=self._ttl,
            heartbeat_seconds=self._heartbeat,
        ) as held:
            try:
                held.assert_healthy()
                current_state = self._git.state(Path(context.session.shared_repo_path))
                if current_state.dirty or current_state.commit != context.run.current_commit:
                    raise ConcurrencyConflict("workspace HEAD is stale or dirty before merge")
                if current_state.commit != record.change_set.base_commit:
                    raise ConcurrencyConflict("ChangeSet base commit is stale")
                expected_index_sha256 = self._git.index_sha256(
                    Path(context.session.shared_repo_path)
                )
                await self._runs.begin_merge_finalizing(
                    context.run.workflow_run_id,
                    workspace_lease=held.lease,
                    lease=context.master_lease,
                )
                finalizing = True
                held.assert_healthy()
                record = await self._change_sets.get_for_source_node(
                    workflow_run_id=context.run.workflow_run_id,
                    source_node_id=source_id,
                )
                approval = await self._approvals.get_for_change_set(record.change_set.change_set_id)
                if (
                    approval is None
                    or approval.approval.status != ApprovalStatus.APPROVED
                    or approval.approval.expires_at <= datetime.now(UTC)
                    or record.change_set.status != ChangeSetStatus.APPROVED
                    or approval.approval.base_commit != record.change_set.base_commit
                    or approval.approval.patch_sha256 != record.change_set.patch_sha256
                    or approval.approval.evidence_sha256
                    != await self._current_evidence_hash(context, record)
                ):
                    raise ConcurrencyConflict("approved merge subject changed before finalization")
                source = next(
                    (node for node in context.run.compiled_snapshot.nodes if node.id == source_id),
                    None,
                )
                if source is None:
                    raise ConcurrencyConflict("merge source disappeared from the snapshot")
                expected_scope = sorted(
                    set((source.effective_allowed_files or []) + (source.effective_new_files or []))
                )
                if sorted(approval.approval.scope) != expected_scope:
                    raise ConcurrencyConflict("approval scope drifted before finalization")
                changed_paths = _paths(record)
                _stored, patch = await self._change_sets.load_patch(record.change_set.change_set_id)
                self._git.apply_check(Path(context.session.shared_repo_path), patch)
                self._git.apply_patch(Path(context.session.shared_repo_path), patch)
                applied = True
                held.assert_healthy()
                committed, _tree = self._git.commit_approved_patch(
                    Path(context.session.shared_repo_path),
                    parent_commit=record.change_set.base_commit,
                    paths=changed_paths,
                    session_id=context.session.session_id,
                    workflow_run_id=context.run.workflow_run_id,
                    change_set_id=record.change_set.change_set_id,
                    approval_id=approval.approval.approval_id,
                    patch_bytes=patch,
                    patch_sha256=record.change_set.patch_sha256,
                    post_state_hash=record.change_set.post_state_hash,
                    expected_index_sha256=expected_index_sha256,
                )
                held.assert_healthy()
                await self._runs.finalize_merge(
                    context.node_run.node_run_id,
                    change_set_id=record.change_set.change_set_id,
                    new_commit=committed,
                    workspace_lease=held.lease,
                    lease=context.master_lease,
                )
                finalizing = False
            except BaseException as error:
                await self._recover_failure(
                    context=context,
                    held=held,
                    changed_paths=changed_paths,
                    committed=committed,
                    finalizing=finalizing,
                    applied=applied,
                    cause=error,
                )
                raise
        return NodeHandlerResult(
            status=NodeRunStatus.COMPLETED,
            outcome=NodeOutcome.SUCCESS,
            summary="Master applied and committed the approved ChangeSet.",
        )

    async def _current_evidence_hash(
        self,
        context: NodeExecutionContext,
        record: object,
    ) -> str:
        if self._artifacts is None:
            return _evidence_hash(record)
        try:
            task = await self._runs.get_task(record.change_set.task_id)
            manifest = await build_manifest(
                run=context.run,
                change_set=record,
                nodes=await self._runs.list_nodes(context.run.workflow_run_id),
                artifacts=self._artifacts,
                runtime_policy_artifact_id=task.runtime_policy_artifact_id,
                capability_lineage=await self._approvals.list_privilege_lineage_for_run(
                    context.run.workflow_run_id
                ),
            )
        except Exception as error:
            raise ConcurrencyConflict("merge evidence cannot be reconstructed") from error
        return manifest_hash(manifest)

    async def _recover_failure(
        self,
        *,
        context: NodeExecutionContext,
        held: HeldWorkspaceLease,
        changed_paths: tuple[str, ...],
        committed: str | None,
        finalizing: bool,
        applied: bool,
        cause: BaseException,
    ) -> None:
        if not finalizing:
            return
        repo = Path(context.session.shared_repo_path)
        try:
            held.assert_healthy()
            state = self._git.state(repo)
            if committed is not None or state.commit != context.run.current_commit:
                raise ChangeSetReconciliationRequired(
                    "merge commit state is ambiguous; recovery evidence is required"
                ) from cause
            if state.dirty:
                record = await self._change_sets.get_for_source_node(
                    workflow_run_id=context.run.workflow_run_id,
                    source_node_id=context.node.source_node_id or "",
                )
                created = (
                    set(record.change_set.created_files)
                    | set(record.change_set.untracked_files)
                    | set(record.change_set.renamed_files)
                    | set(record.change_set.created_directories)
                )
                existing = tuple(path for path in changed_paths if path not in created)

                def restore() -> None:
                    self._git.restore_existing_paths(
                        repo,
                        base_commit=record.change_set.base_commit,
                        paths=existing,
                    )
                    self._git.unstage_new_paths(repo, tuple(sorted(created)))
                    self._git.remove_worktree_entries(repo, tuple(sorted(created)))

                await self._locks.run_fenced(
                    held,
                    session_id=context.run.session_id,
                    ttl_seconds=self._ttl,
                    operation=restore,
                )
            clean = self._git.state(repo)
            if clean.dirty or clean.commit != context.run.current_commit:
                raise ChangeSetReconciliationRequired(
                    "merge failure did not restore the expected clean HEAD"
                ) from cause
            await self._runs.clear_merge_finalizing(
                context.run.workflow_run_id,
                workspace_lease=held.lease,
                lease=context.master_lease,
            )
        except BaseException as recovery_error:
            if isinstance(recovery_error, ChangeSetReconciliationRequired):
                raise
            raise ChangeSetReconciliationRequired(
                "merge failure cleanup requires recovery"
            ) from recovery_error


__all__ = ["MergePatchNodeHandler"]
