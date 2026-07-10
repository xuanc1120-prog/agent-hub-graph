"""Frozen v1 HTTP API data contracts.

These are the cross-module request/response shapes that both the FastAPI
backend (owned by the runtime) and the React frontend depend on. Every model is
strict (``extra="forbid"``); the backend declares an independent ``response_model``
per route and never returns bare dicts, DB rows or internal paths.

Concurrency-control fields are frozen here so client and server agree on their
names and semantics:

* ``expected_semantic_version`` guards AuthorGraph mutations.
* ``expected_layout_version`` guards layout autosave.
* ``confirmed_compiled_hash`` + ``expected_semantic_version`` gate ``run``.
* ``approval_version`` + ``confirm_subject_hash`` gate approve/reject/renew.
* ``expected_fencing_token`` gates workspace recovery.

The ``Idempotency-Key`` header (not modeled here) is required on every
retryable mutation.
"""

from __future__ import annotations

from pydantic import Field

from protocol.common import (
    EntityId,
    GitObjectId,
    RiskLevel,
    Sha256Hex,
    ShortReasonText,
    StrictModel,
)
from protocol.workflow import AuthorGraph, CompiledGraph, WorkflowLayout

# --- Shared value objects -------------------------------------------------------


class ValidationIssue(StrictModel):
    """A single error or warning surfaced by validate/compile."""

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=2_000)
    node_id: EntityId | None = None
    edge_id: EntityId | None = None


class PageInfo(StrictModel):
    """Cursor pagination envelope shared by all list responses."""

    next_cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(ge=1, le=500)


# --- Workflow save / layout -----------------------------------------------------


class WorkflowSaveRequest(StrictModel):
    author_graph: AuthorGraph
    expected_semantic_version: int = Field(ge=0)


class WorkflowSaveResponse(StrictModel):
    workflow_id: EntityId
    semantic_version: int = Field(ge=1)


class LayoutSaveRequest(StrictModel):
    layout: WorkflowLayout
    expected_layout_version: int = Field(ge=0)


class LayoutSaveResponse(StrictModel):
    workflow_id: EntityId
    layout_version: int = Field(ge=1)


# --- Validate -------------------------------------------------------------------


class ValidateRequest(StrictModel):
    expected_semantic_version: int = Field(ge=0)


class ValidateResponse(StrictModel):
    ok: bool
    errors: list[ValidationIssue] = Field(default_factory=list, max_length=500)
    warnings: list[ValidationIssue] = Field(default_factory=list, max_length=500)
    compiled_graph: CompiledGraph | None = None
    compiled_hash: Sha256Hex | None = None
    integration_base_commit: GitObjectId | None = None
    agent_catalog_hash: Sha256Hex | None = None
    policy_version: str | None = Field(default=None, min_length=1, max_length=64)
    source_semantic_version: int | None = Field(default=None, ge=1)


# --- Run ------------------------------------------------------------------------


class RunRequest(StrictModel):
    expected_semantic_version: int = Field(ge=1)
    confirmed_compiled_hash: Sha256Hex


class RunResponse(StrictModel):
    workflow_run_id: EntityId


# --- Assignment -----------------------------------------------------------------


class AssignAgentRequest(StrictModel):
    agent_id: EntityId
    expected_semantic_version: int = Field(ge=1)


class LockNodeRequest(StrictModel):
    locked: bool
    expected_semantic_version: int = Field(ge=1)


class MutationVersionResponse(StrictModel):
    workflow_id: EntityId
    semantic_version: int = Field(ge=1)


# --- Approvals ------------------------------------------------------------------


class ApprovalDecisionRequest(StrictModel):
    approval_version: int = Field(ge=1)
    confirm_subject_hash: Sha256Hex


class ApprovalRenewRequest(StrictModel):
    approval_version: int = Field(ge=1)
    confirm_subject_hash: Sha256Hex


# --- Planning -------------------------------------------------------------------


class PlanRequest(StrictModel):
    planner_mode: str = Field(pattern=r"^(open_code|rule_based)$")
    parent_workflow_id: EntityId | None = None


class PlanResponse(StrictModel):
    planner_run_id: EntityId


# --- Workspace recovery ---------------------------------------------------------


class RecoverWorkspaceRequest(StrictModel):
    expected_fencing_token: int = Field(ge=1)
    resolution: str = Field(pattern=r"^(retry|cancel)$")


# --- WebSocket ticket -----------------------------------------------------------


class WsTicketResponse(StrictModel):
    ticket: str = Field(min_length=1, max_length=512)
    expires_in_seconds: int = Field(ge=1, le=300)


# --- Risk panel -----------------------------------------------------------------


class RiskFinding(StrictModel):
    """De-identified risk summary shown in the review panel."""

    risk_level: RiskLevel
    reason: ShortReasonText
    path: str | None = Field(default=None, max_length=1_024)
