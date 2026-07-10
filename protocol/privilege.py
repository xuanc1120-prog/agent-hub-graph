"""Frozen v1 approval, privilege and capability-grant contracts.

Two approval subtypes are discriminated by ``subject_type`` and each binds its
own immutable subject hash, scope and expiry. A ``PrivilegeApproval`` never
fabricates a change_set. ``CapabilityGrant`` is a one-shot, time- and
scope-limited authorization bound to exactly one target task and one existing
resource; it never rewrites compiled effective scope.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from protocol.common import (
    CapabilityType,
    EntityId,
    GitObjectId,
    PrivilegeAction,
    ReasonText,
    RepoRelativePath,
    RiskLevel,
    Sha256Hex,
    ShortReasonText,
    StrictModel,
    SummaryText,
)
from protocol.result import NextSuggestion


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


class PrivilegeRequestStatus(StrEnum):
    PENDING = "pending"
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    DENIED = "denied"


class ApprovalBase(StrictModel):
    approval_id: EntityId
    workflow_run_id: EntityId
    node_run_id: EntityId
    subject_sha256: Sha256Hex
    effective_risk: RiskLevel
    scope: list[RepoRelativePath] = Field(default_factory=list, max_length=500)
    status: ApprovalStatus = ApprovalStatus.PENDING
    version: int = Field(default=1, ge=1)
    expires_at: datetime


class ChangeSetApproval(ApprovalBase):
    subject_type: Literal["change_set"] = "change_set"
    change_set_id: EntityId
    base_commit: GitObjectId
    patch_sha256: Sha256Hex
    evidence_sha256: Sha256Hex


class PrivilegeApproval(ApprovalBase):
    subject_type: Literal["privilege_request"] = "privilege_request"
    privilege_request_id: EntityId
    evidence_sha256: Sha256Hex


Approval = Annotated[
    ChangeSetApproval | PrivilegeApproval,
    Field(discriminator="subject_type"),
]


class PrivilegeRequestProposal(StrictModel):
    requested_capability: CapabilityType
    requested_action: PrivilegeAction
    requested_resource: RepoRelativePath | None = None
    reason: ReasonText = ""
    expected_impact: list[ShortReasonText] = Field(default_factory=list, max_length=50)
    related_files: list[RepoRelativePath] = Field(default_factory=list, max_length=100)
    rollback_plan: ReasonText | None = None
    risk_level_hint: RiskLevel = RiskLevel.L2


class PrivilegeRequest(PrivilegeRequestProposal):
    request_id: EntityId
    session_id: EntityId
    task_id: EntityId
    node_run_id: EntityId
    agent_id: EntityId
    requested_resource: RepoRelativePath
    effective_risk: RiskLevel = RiskLevel.L2
    status: PrivilegeRequestStatus = PrivilegeRequestStatus.PENDING


class AgentOutputEnvelope(StrictModel):
    summary: SummaryText = ""
    risk_hints: list[ShortReasonText] = Field(default_factory=list, max_length=50)
    next_suggestion: NextSuggestion | None = None
    privilege_requests: list[PrivilegeRequestProposal] = Field(default_factory=list, max_length=1)


class CapabilityGrant(StrictModel):
    grant_id: EntityId
    request_id: EntityId
    target_task_id: EntityId
    action: PrivilegeAction
    resource: RepoRelativePath
    expires_at: datetime
    consumed_at: datetime | None = None
    consumed_fencing_token: int | None = Field(default=None, ge=1)
    revoked_at: datetime | None = None
    revocation_reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _check_consume_revoke_invariants(self) -> Self:
        if (self.consumed_at is None) != (self.consumed_fencing_token is None):
            raise ValueError("consumed_at and consumed_fencing_token must be set together")
        if (self.revoked_at is None) != (self.revocation_reason is None):
            raise ValueError("revoked_at and revocation_reason must be set together")
        if self.consumed_at is not None and self.revoked_at is not None:
            raise ValueError("a grant cannot be both consumed and revoked")
        return self
