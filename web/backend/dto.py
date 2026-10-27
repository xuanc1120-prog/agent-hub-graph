"""Strict HTTP-only DTOs for the first local Mock API slice."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from protocol import (
    ApprovalStatus,
    ArtifactType,
    AuthorGraph,
    ChangeSetStatus,
    EntityId,
    GitObjectId,
    NodeOutcome,
    NodeRunStatus,
    NodeType,
    PlannerRunStatus,
    RiskLevel,
    SessionStatus,
    Sha256Hex,
    StrictModel,
    WorkflowLayout,
    WorkflowRunStatus,
)
from protocol.api import ValidateResponse


class SessionCreateRequest(StrictModel):
    repository: str = Field(min_length=1, max_length=4_096)
    goal: str = Field(min_length=1, max_length=20_000)


class SessionResponse(StrictModel):
    session_id: EntityId
    status: SessionStatus
    base_commit: GitObjectId
    integration_head_commit: GitObjectId


class PlanRequest(StrictModel):
    """Empty by design: this slice only queues the deterministic Mock planner."""


class PlanAcceptedResponse(StrictModel):
    planner_run_id: EntityId
    status: PlannerRunStatus


class PlannerRunResponse(StrictModel):
    planner_run_id: EntityId
    session_id: EntityId
    status: PlannerRunStatus
    result_workflow_id: EntityId | None = None
    result_semantic_version: int | None = Field(default=None, ge=1)
    error_code: str | None = Field(default=None, max_length=200)


class WorkflowResponse(StrictModel):
    workflow_id: EntityId
    session_id: EntityId
    semantic_version: int = Field(ge=1)
    layout_version: int = Field(ge=1)
    author_graph: AuthorGraph
    layout: WorkflowLayout


class WorkflowUpdateRequest(StrictModel):
    expected_semantic_version: int = Field(ge=1)
    author_graph: AuthorGraph


class WorkflowUpdateResponse(StrictModel):
    workflow_id: EntityId
    semantic_version: int = Field(ge=1)


class RunAcceptedResponse(StrictModel):
    workflow_run_id: EntityId
    status: WorkflowRunStatus


class NodeStatusResponse(StrictModel):
    node_run_id: EntityId
    node_id: EntityId
    node_type: NodeType
    attempt: int = Field(ge=1)
    status: NodeRunStatus
    outcome: NodeOutcome | None = None
    error_code: str | None = Field(default=None, max_length=200)
    output_artifact_id: EntityId | None = None


class ApprovalResponse(StrictModel):
    approval_id: EntityId
    workflow_run_id: EntityId
    subject_sha256: Sha256Hex
    status: ApprovalStatus
    version: int = Field(ge=1)
    expires_at: datetime
    effective_risk: RiskLevel
    change_set_id: EntityId
    base_commit: GitObjectId
    patch_sha256: Sha256Hex
    evidence_sha256: Sha256Hex
    scope: list[str] = Field(max_length=500)


class WorkflowRunResponse(StrictModel):
    workflow_run_id: EntityId
    workflow_id: EntityId
    session_id: EntityId
    status: WorkflowRunStatus
    current_commit: GitObjectId
    compiled_snapshot_hash: Sha256Hex
    nodes: list[NodeStatusResponse] = Field(max_length=1_000)
    approvals: list[ApprovalResponse] = Field(max_length=1_000)


class ChangeSetResponse(StrictModel):
    change_set_id: EntityId
    session_id: EntityId
    workflow_run_id: EntityId
    status: ChangeSetStatus
    base_commit: GitObjectId
    patch_sha256: Sha256Hex
    patch_artifact_id: EntityId
    patch_size_bytes: int = Field(ge=0)
    created_files: list[str] = Field(max_length=500)
    modified_files: list[str] = Field(max_length=500)
    deleted_files: list[str] = Field(max_length=500)
    renamed_files: list[str] = Field(max_length=500)


class ArtifactMetadataResponse(StrictModel):
    artifact_id: EntityId
    artifact_type: ArtifactType
    sha256: Sha256Hex
    size_bytes: int = Field(ge=0)
    redacted: bool


class ArtifactListResponse(StrictModel):
    items: list[ArtifactMetadataResponse] = Field(max_length=500)


class ApprovalDecisionRequest(StrictModel):
    approval_version: int = Field(ge=1)
    confirm_subject_hash: Sha256Hex


__all__ = [
    "ApprovalDecisionRequest",
    "ApprovalResponse",
    "ArtifactListResponse",
    "ArtifactMetadataResponse",
    "ChangeSetResponse",
    "NodeStatusResponse",
    "PlanAcceptedResponse",
    "PlanRequest",
    "PlannerRunResponse",
    "RunAcceptedResponse",
    "SessionCreateRequest",
    "SessionResponse",
    "ValidateResponse",
    "WorkflowResponse",
    "WorkflowRunResponse",
    "WorkflowUpdateRequest",
    "WorkflowUpdateResponse",
]
