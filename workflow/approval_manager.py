"""Approval orchestration for immutable ChangeSet subjects."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from protocol import (
    ApprovalStatus,
    ChangeSetApproval,
    RiskLevel,
)
from storage.approval_repository import ApprovalRecord, ApprovalRepository
from storage.artifact_repository import ArtifactRepository
from storage.change_set_repository import ChangeSetRepository
from storage.errors import ConcurrencyConflict
from storage.leases import MasterLease
from storage.workflow_run_repository import WorkflowRunRepository
from workflow.approval_evidence import build_manifest, manifest_hash
from workflow.handlers.base import NodeExecutionContext


class ApprovalPending(RuntimeError):
    def __init__(self, approval_id: str, workflow_run_id: str, node_run_id: str) -> None:
        super().__init__(f"approval pending: {approval_id}")
        self.approval_id = approval_id
        self.workflow_run_id = workflow_run_id
        self.node_run_id = node_run_id


class PrivilegePending(ApprovalPending):
    """An AgentTask must pause until its side-gate approval is granted."""


def _subject_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _max_risk(*values: RiskLevel) -> RiskLevel:
    return max(values, key=lambda item: list(RiskLevel).index(item))


class ApprovalManager:
    def __init__(
        self,
        approvals: ApprovalRepository,
        change_sets: ChangeSetRepository,
        runs: WorkflowRunRepository,
        *,
        ttl_seconds: int = 3600,
        artifacts: ArtifactRepository | None = None,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("approval ttl must be positive")
        self._approvals = approvals
        self._change_sets = change_sets
        self._runs = runs
        self._artifacts = artifacts
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def subject_hash(value: dict[str, Any]) -> str:
        return _subject_hash(value)

    async def prepare_changeset(
        self,
        context: NodeExecutionContext,
        *,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        source_id = context.node.source_node_id
        if source_id is None:
            raise ValueError("approval node has no source node")
        record = await self._change_sets.get_for_source_node(
            workflow_run_id=context.run.workflow_run_id,
            source_node_id=source_id,
        )
        source = next(
            (node for node in context.run.compiled_snapshot.nodes if node.id == source_id),
            None,
        )
        if source is None:
            raise ValueError("approval source node is absent from snapshot")
        if self._artifacts is None:
            evidence_sha256 = sha256(
                json.dumps(
                    sorted(
                        (ref.artifact_id, ref.sha256, ref.artifact_type.value)
                        for ref in record.change_set.evidence_refs
                    ),
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            manifest_effective_risk = None
        else:
            try:
                nodes = await self._runs.list_nodes(context.run.workflow_run_id)
                task = await self._runs.get_task(record.change_set.task_id)
                manifest = await build_manifest(
                    run=context.run,
                    change_set=record,
                    nodes=nodes,
                    artifacts=self._artifacts,
                    runtime_policy_artifact_id=task.runtime_policy_artifact_id,
                    capability_lineage=await self._approvals.list_privilege_lineage_for_run(
                        context.run.workflow_run_id
                    ),
                )
            except Exception as error:
                raise ConcurrencyConflict("approval evidence cannot be reconstructed") from error
            evidence_sha256 = manifest_hash(manifest)
            manifest_effective_risk = RiskLevel(manifest["effective_risk"])
        scope = sorted(
            set((source.effective_allowed_files or []) + (source.effective_new_files or []))
        )
        effective_risk = _max_risk(
            source.policy_risk_floor or RiskLevel.L1,
            source.risk_level_hint,
            manifest_effective_risk or RiskLevel.L0,
        )
        expiry = (now or datetime.now(UTC)) + timedelta(seconds=self._ttl_seconds)
        subject = {
            "workflow_run_id": context.run.workflow_run_id,
            "node_run_id": context.node_run.node_run_id,
            "change_set_id": record.change_set.change_set_id,
            "base_commit": record.change_set.base_commit,
            "patch_sha256": record.change_set.patch_sha256,
            "evidence_sha256": evidence_sha256,
            "scope": scope,
            "effective_risk": effective_risk.value,
            "post_state_hash": record.change_set.post_state_hash,
            "compiled_snapshot_hash": context.run.compiled_snapshot_hash,
            "policy_version": context.run.policy_version,
            "current_commit": context.run.current_commit,
            "expires_at": expiry.isoformat().replace("+00:00", "Z"),
        }
        subject_sha256 = _subject_hash(subject)
        approval = ChangeSetApproval(
            approval_id=f"approval-cs-{subject_sha256[:32]}",
            workflow_run_id=context.run.workflow_run_id,
            node_run_id=context.node_run.node_run_id,
            subject_sha256=subject_sha256,
            effective_risk=effective_risk,
            scope=scope,
            expires_at=expiry,
            change_set_id=record.change_set.change_set_id,
            base_commit=record.change_set.base_commit,
            patch_sha256=record.change_set.patch_sha256,
            evidence_sha256=evidence_sha256,
        )
        latest = await self._approvals.get_for_change_set(record.change_set.change_set_id)
        if latest is not None and latest.approval.status == ApprovalStatus.PENDING:
            current_now = now or datetime.now(UTC)
            if latest.approval.expires_at <= current_now:
                await self._approvals.expire_due(
                    master_lease=context.master_lease,
                    workflow_run_id=context.run.workflow_run_id,
                    now=current_now,
                )
                latest = await self._approvals.get_for_change_set(record.change_set.change_set_id)
        if latest is not None and latest.approval.status in {
            ApprovalStatus.PENDING,
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
        }:
            if not isinstance(latest.approval, ChangeSetApproval):
                raise ConcurrencyConflict("ChangeSet approval subject type drifted")
            if (
                latest.approval.change_set_id != approval.change_set_id
                or latest.approval.base_commit != approval.base_commit
                or latest.approval.patch_sha256 != approval.patch_sha256
                or latest.approval.evidence_sha256 != approval.evidence_sha256
                or latest.approval.effective_risk != approval.effective_risk
                or latest.approval.scope != approval.scope
            ):
                raise ConcurrencyConflict("ChangeSet approval subject drifted")
            if latest.approval.status == ApprovalStatus.APPROVED and latest.approval.expires_at <= (
                now or datetime.now(UTC)
            ):
                raise ConcurrencyConflict("ChangeSet approval has expired")
            return latest
        return await self._approvals.create(
            approval=approval,
            master_lease=context.master_lease,
            now=now,
        )

    async def execute(self, context: NodeExecutionContext) -> ApprovalRecord:
        record = await self.prepare_changeset(context)
        if record.approval.status == ApprovalStatus.PENDING:
            raise ApprovalPending(
                record.approval.approval_id,
                record.approval.workflow_run_id,
                record.approval.node_run_id,
            )
        return record

    async def decide(
        self,
        approval_id: str,
        *,
        expected_version: int,
        confirm_subject_hash: str,
        approved: bool,
        actor_id: str,
        idempotency_key: str,
        master_lease: MasterLease,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        target = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        result = await self._approvals.decide(
            approval_id,
            expected_version=expected_version,
            confirm_subject_hash=confirm_subject_hash,
            target=target,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            master_lease=master_lease,
            now=now,
        )
        # The repository advances a ChangeSet approval gate in the same SQL
        # transaction as the decision.  The fallback below is retained only
        # for older injected repositories used by external callers/tests.
        if not isinstance(result.approval, ChangeSetApproval):
            return result
        if getattr(self._approvals, "atomic_resume_available", False):
            return result
        try:
            await self._runs.resume_after_approval(
                result.approval.workflow_run_id,
                result.approval.node_run_id,
                approved=approved,
                lease=master_lease,
                now=now,
            )
        except ConcurrencyConflict:
            run = await self._runs.get(result.approval.workflow_run_id)
            node = next(
                item
                for item in await self._runs.list_nodes(result.approval.workflow_run_id)
                if item.node_run_id == result.approval.node_run_id
            )
            if run.status.value not in {"running", "completed", "cancelled"} and (
                node.status.value == "waiting_approval"
            ):
                raise
        return result

    async def approve(self, approval_id: str, **kwargs: Any) -> ApprovalRecord:
        return await self.decide(approval_id, approved=True, **kwargs)

    async def reject(self, approval_id: str, **kwargs: Any) -> ApprovalRecord:
        return await self.decide(approval_id, approved=False, **kwargs)

    async def expire(
        self,
        *,
        master_lease: MasterLease,
        workflow_run_id: str | None = None,
        now: datetime | None = None,
    ) -> list[ApprovalRecord]:
        return await self._approvals.expire_due(
            master_lease=master_lease,
            workflow_run_id=workflow_run_id,
            now=now,
        )

    async def renew(
        self,
        *,
        old_approval_id: str,
        expected_version: int,
        confirm_subject_hash: str,
        new_approval: ChangeSetApproval,
        master_lease: MasterLease,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        old = await self._approvals.get(old_approval_id)
        if not isinstance(old.approval, ChangeSetApproval):
            raise ValueError("only ChangeSetApproval can be renewed")
        change_set = await self._change_sets.get(old.approval.change_set_id)
        run = await self._runs.get(old.approval.workflow_run_id)
        nodes = await self._runs.list_nodes(run.workflow_run_id)
        source_record = next(
            (item for item in nodes if item.node_run_id == change_set.change_set.node_run_id),
            None,
        )
        if source_record is None:
            raise ConcurrencyConflict("renewal source run is absent from the workflow")
        source = next(
            (node for node in run.compiled_snapshot.nodes if node.id == source_record.node_id),
            None,
        )
        if source is None:
            raise ConcurrencyConflict("renewal source is absent from the immutable snapshot")
        if self._artifacts is None:
            evidence_sha256 = old.approval.evidence_sha256
            manifest_effective_risk = None
        else:
            try:
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
            except Exception as error:
                raise ConcurrencyConflict("renewal evidence cannot be reconstructed") from error
            evidence_sha256 = manifest_hash(manifest)
            manifest_effective_risk = RiskLevel(manifest["effective_risk"])
        effective_risk = _max_risk(
            source.policy_risk_floor or RiskLevel.L1,
            source.risk_level_hint,
            manifest_effective_risk or RiskLevel.L0,
        )
        expected_subject = _subject_hash(
            {
                "workflow_run_id": run.workflow_run_id,
                "node_run_id": new_approval.node_run_id,
                "change_set_id": change_set.change_set.change_set_id,
                "base_commit": change_set.change_set.base_commit,
                "patch_sha256": change_set.change_set.patch_sha256,
                "evidence_sha256": evidence_sha256,
                "scope": sorted(
                    set((source.effective_allowed_files or []) + (source.effective_new_files or []))
                ),
                "effective_risk": effective_risk.value,
                "post_state_hash": change_set.change_set.post_state_hash,
                "compiled_snapshot_hash": run.compiled_snapshot_hash,
                "policy_version": run.policy_version,
                "current_commit": run.current_commit,
                "expires_at": new_approval.expires_at.astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
        if new_approval.subject_sha256 != expected_subject:
            raise ValueError("renewal subject hash does not match its immutable subject")
        return await self._approvals.renew_changeset(
            old_approval_id=old_approval_id,
            expected_version=expected_version,
            confirm_subject_hash=confirm_subject_hash,
            new_approval=new_approval,
            master_lease=master_lease,
            now=now,
        )


__all__ = ["ApprovalManager", "ApprovalPending", "PrivilegePending"]
