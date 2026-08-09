"""Runtime capability side-gate with exact resource and task bindings."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from protocol import (
    CapabilityGrant,
    CapabilityType,
    PrivilegeAction,
    PrivilegeApproval,
    PrivilegeRequest,
    PrivilegeRequestProposal,
    PrivilegeRequestStatus,
    RiskLevel,
)
from storage.approval_repository import (
    ApprovalRecord,
    ApprovalRepository,
    CapabilityGrantRecord,
    PrivilegeRequestBinding,
)
from storage.db import Transaction
from storage.leases import MasterLease, WorkspaceLease
from storage.workflow_run_repository import task_id_for_node
from workflow.handlers.base import NodeExecutionContext

_ACTIONS = {
    CapabilityType.MODIFY_DEPENDENCY: PrivilegeAction.EDIT_DEPENDENCY_MANIFEST,
    CapabilityType.MODIFY_CONFIG: PrivilegeAction.EDIT_PROJECT_CONFIG,
}


def _risk(value: RiskLevel, floor: RiskLevel) -> RiskLevel:
    return max(value, floor, key=lambda item: list(RiskLevel).index(item))


def _hash(value: dict[str, Any]) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class CapabilityBroker:
    def __init__(
        self,
        approvals: ApprovalRepository,
        *,
        ttl_seconds: int = 900,
        grant_ttl_seconds: int | None = None,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("grant ttl must be positive")
        self._approvals = approvals
        self._approval_ttl_seconds = ttl_seconds
        self._grant_ttl_seconds = grant_ttl_seconds or ttl_seconds
        if self._grant_ttl_seconds < 1:
            raise ValueError("grant ttl must be positive")

    async def request(
        self,
        context: NodeExecutionContext,
        proposal: PrivilegeRequestProposal,
        *,
        now: datetime | None = None,
    ) -> tuple[PrivilegeRequest, ApprovalRecord | None]:
        expected_action = _ACTIONS.get(proposal.requested_capability)
        if expected_action != proposal.requested_action:
            raise ValueError("capability and action are not a permitted pair")
        resource = proposal.requested_resource
        if not resource:
            raise ValueError("privilege requests require one exact existing resource")
        source = context.node
        allowed = set(source.effective_allowed_files or [])
        if resource not in allowed or resource in set(source.effective_new_files or []):
            raise ValueError("privilege resource is outside the compiled existing-file scope")
        repo_root = Path(context.session.shared_repo_path).expanduser().resolve(strict=True)
        candidate = repo_root / resource
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or candidate.resolve(strict=True) != candidate.absolute()
        ):
            raise ValueError("privilege resource must be one existing non-reparse file")
        effective_risk = _risk(
            proposal.risk_level_hint,
            context.node.policy_risk_floor or RiskLevel.L1,
        )
        expiry = (now or datetime.now(UTC)) + timedelta(seconds=self._approval_ttl_seconds)
        runtime_policy = {
            "mode": "shared_write",
            "agent_id": context.node.resolved_agent_id or "unknown",
            "node_id": context.node.id,
            "allowed_existing_files": sorted(source.effective_allowed_files or []),
            "allowed_new_files": sorted(source.effective_new_files or []),
            "allowed_commands": [
                list(command) for command in (source.effective_allowed_commands or [])
            ],
            "write": source.requires_write,
            "network": False,
            "git_push": False,
            "git_merge": False,
        }
        subject = {
            "schema": "hub210-privilege-subject-v1",
            "workflow_run_id": context.run.workflow_run_id,
            "session_id": context.run.session_id,
            "node_run_id": context.node_run.node_run_id,
            "task_id": task_id_for_node(context.node_run.node_run_id),
            "attempt": context.node_run.attempt,
            "compiled_snapshot_hash": context.run.compiled_snapshot_hash,
            "layout_snapshot_hash": context.run.layout_snapshot_hash,
            "policy_version": context.run.policy_version,
            "current_commit": context.run.current_commit,
            "runtime_policy": runtime_policy,
            "capability": proposal.requested_capability.value,
            "action": proposal.requested_action.value,
            "resource": resource,
            "reason": proposal.reason,
            "expected_impact": proposal.expected_impact,
            "related_files": proposal.related_files,
            "rollback_plan": proposal.rollback_plan,
            "effective_risk": effective_risk.value,
            "expires_at": expiry.isoformat().replace("+00:00", "Z"),
        }
        subject_sha256 = _hash(subject)
        request = PrivilegeRequest(
            request_id=f"priv-{subject_sha256[:32]}",
            session_id=context.run.session_id,
            task_id=subject["task_id"],
            node_run_id=context.node_run.node_run_id,
            agent_id=context.node.resolved_agent_id or "unknown",
            requested_capability=proposal.requested_capability,
            requested_action=proposal.requested_action,
            requested_resource=resource,
            reason=proposal.reason,
            expected_impact=proposal.expected_impact,
            related_files=proposal.related_files,
            rollback_plan=proposal.rollback_plan,
            risk_level_hint=proposal.risk_level_hint,
            effective_risk=effective_risk,
            status=(
                PrivilegeRequestStatus.DENIED
                if effective_risk == RiskLevel.L4
                else PrivilegeRequestStatus.PENDING
            ),
        )
        await self._approvals.create_privilege_request(
            request=request,
            master_lease=context.master_lease,
            now=now,
        )
        if effective_risk == RiskLevel.L4:
            return request, None
        approval = PrivilegeApproval(
            approval_id=f"approval-priv-{subject_sha256[:32]}",
            workflow_run_id=context.run.workflow_run_id,
            node_run_id=context.node_run.node_run_id,
            subject_sha256=subject_sha256,
            effective_risk=effective_risk,
            scope=[resource],
            expires_at=expiry,
            privilege_request_id=request.request_id,
            evidence_sha256=sha256(
                json.dumps(subject, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        )
        return request, await self._approvals.create(
            approval=approval,
            master_lease=context.master_lease,
            now=now,
        )

    async def issue_grant(
        self,
        grant: CapabilityGrant,
        *,
        workflow_run_id: str,
        source_task_id: str,
        master_lease: MasterLease,
        now: datetime | None = None,
    ) -> CapabilityGrantRecord:
        if grant.action not in {
            PrivilegeAction.EDIT_DEPENDENCY_MANIFEST,
            PrivilegeAction.EDIT_PROJECT_CONFIG,
        }:
            raise ValueError("unsupported capability action")
        current = now or datetime.now(UTC)
        if grant.expires_at <= current:
            raise ValueError("capability grant must expire in the future")
        if grant.expires_at > current + timedelta(seconds=self._grant_ttl_seconds):
            raise ValueError("capability grant exceeds the configured lifetime")
        return await self._approvals.create_grant(
            grant=grant,
            workflow_run_id=workflow_run_id,
            source_task_id=source_task_id,
            master_lease=master_lease,
            now=now,
        )

    async def consume(
        self,
        grant_id: str,
        *,
        target_task_id: str,
        action: PrivilegeAction,
        resource: str,
        master_lease: MasterLease,
        workspace_lease: WorkspaceLease,
        now: datetime | None = None,
    ) -> CapabilityGrantRecord:
        return await self._approvals.consume_grant(
            grant_id=grant_id,
            target_task_id=target_task_id,
            action=action,
            resource=resource,
            master_lease=master_lease,
            workspace_lease=workspace_lease,
            now=now,
        )

    async def grant_for_task(self, task_id: str) -> CapabilityGrantRecord | None:
        return await self._approvals.get_grant_for_task(task_id)

    async def approval_for_request(self, request_id: str) -> ApprovalRecord | None:
        return await self._approvals.get_for_privilege_request(request_id)

    async def validate_result_requests(
        self,
        context: NodeExecutionContext,
        *,
        task_id: str,
        request_ids: list[str],
    ) -> PrivilegeRequestBinding:
        if not context.node.requires_write:
            raise ValueError("privilege requests require a compiled write task")
        if len(request_ids) != 1:
            raise ValueError("a privilege result must identify exactly one request")
        binding = await self._approvals.get_privilege_request_binding(request_ids[0])
        if binding is None:
            raise ValueError("privilege request is not persisted")
        if (
            binding.workflow_run_id != context.run.workflow_run_id
            or binding.session_id != context.run.session_id
            or binding.node_run_id != context.node_run.node_run_id
            or binding.task_id != task_id
            or binding.agent_id != (context.node.resolved_agent_id or "")
            or binding.status
            not in {PrivilegeRequestStatus.PENDING, PrivilegeRequestStatus.WAITING_APPROVAL}
        ):
            raise ValueError("privilege request is not bound to the active AgentTask")
        allowed = set(context.node.effective_allowed_files or [])
        if binding.resource not in allowed or binding.resource in set(
            context.node.effective_new_files or []
        ):
            raise ValueError("privilege request resource exceeds compiled existing-file scope")
        return binding

    async def revoke_for_run(
        self,
        workflow_run_id: str,
        *,
        master_lease: MasterLease,
        reason: str = "workflow_cancelled",
        now: datetime | None = None,
    ) -> int:
        return await self._approvals.revoke_unconsumed_for_run(
            workflow_run_id=workflow_run_id,
            master_lease=master_lease,
            reason=reason,
            now=now,
        )

    async def revoke_for_run_in(
        self,
        transaction: Transaction,
        workflow_run_id: str,
        *,
        master_lease: MasterLease,
        reason: str = "workflow_cancelled",
        now: datetime | None = None,
    ) -> int:
        return await self._approvals.revoke_unconsumed_for_run_in(
            transaction,
            workflow_run_id=workflow_run_id,
            master_lease=master_lease,
            reason=reason,
            now=now,
        )


__all__ = ["CapabilityBroker"]
