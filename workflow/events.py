"""Typed durable scheduler event payloads."""

from __future__ import annotations

from pydantic import Field

from protocol import (
    ApprovalStatus,
    ChangeSetStatus,
    EntityId,
    NodeOutcome,
    NodeRunStatus,
    PlannerRunStatus,
    Sha256Hex,
    StrictModel,
    SummaryText,
    TaskStatus,
    WorkflowRunStatus,
)
from storage.event_registry import EventRegistry

RUN_CREATED = "workflow.run_created"
RUN_STATE_CHANGED = "workflow.state_changed"
NODE_STATE_CHANGED = "workflow.node_state_changed"
TASK_STATE_CHANGED = "workflow.task_state_changed"
PLANNER_STATE_CHANGED = "workflow.planner_state_changed"
CHANGE_SET_STATE_CHANGED = "workflow.change_set_state_changed"
APPROVAL_STATE_CHANGED = "workflow.approval_state_changed"
PRIVILEGE_REQUEST_STATE_CHANGED = "workflow.privilege_request_state_changed"
CAPABILITY_GRANT_STATE_CHANGED = "workflow.capability_grant_state_changed"
RECOVERY_ACTION = "workflow.recovery_action"


class WorkflowRunEventPayload(StrictModel):
    master_fencing_token: int = Field(ge=1)
    workflow_run_id: EntityId
    previous_status: WorkflowRunStatus | None = None
    status: WorkflowRunStatus
    compiled_snapshot_hash: Sha256Hex | None = None
    planner_run_id: EntityId | None = None
    planner_id: EntityId | None = None
    planner_model: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=1_000)


class NodeRunEventPayload(StrictModel):
    master_fencing_token: int = Field(ge=1)
    workflow_run_id: EntityId
    node_run_id: EntityId
    node_id: EntityId
    previous_status: NodeRunStatus
    status: NodeRunStatus
    outcome: NodeOutcome | None = None
    summary: SummaryText = ""
    error_code: str | None = Field(default=None, max_length=200)


class TaskEventPayload(StrictModel):
    master_fencing_token: int = Field(ge=1)
    workspace_fencing_token: int | None = Field(default=None, ge=1)
    workflow_run_id: EntityId
    node_run_id: EntityId
    task_id: EntityId
    previous_status: TaskStatus | None = None
    status: TaskStatus
    error_code: str | None = Field(default=None, max_length=200)


class PlannerRunEventPayload(StrictModel):
    master_fencing_token: int = Field(ge=1)
    planner_run_id: EntityId
    previous_status: PlannerRunStatus | None = None
    status: PlannerRunStatus
    error_code: str | None = Field(default=None, max_length=200)


class ChangeSetEventPayload(StrictModel):
    master_fencing_token: int = Field(ge=1)
    workspace_fencing_token: int | None = Field(default=None, ge=1)
    workflow_run_id: EntityId
    node_run_id: EntityId
    task_id: EntityId
    change_set_id: EntityId
    previous_status: ChangeSetStatus | None = None
    status: ChangeSetStatus
    patch_sha256: Sha256Hex
    reason: str | None = Field(default=None, max_length=1_000)


class ApprovalEventPayload(StrictModel):
    master_fencing_token: int = Field(ge=1)
    workflow_run_id: EntityId
    node_run_id: EntityId
    approval_id: EntityId
    subject_type: str = Field(pattern=r"^(change_set|privilege_request)$")
    previous_status: ApprovalStatus | None = None
    status: ApprovalStatus
    version: int = Field(ge=1)
    subject_sha256: Sha256Hex
    reason: str | None = Field(default=None, max_length=1_000)


class PrivilegeRequestEventPayload(StrictModel):
    master_fencing_token: int = Field(ge=1)
    workflow_run_id: EntityId
    node_run_id: EntityId
    task_id: EntityId
    request_id: EntityId
    previous_status: str | None = Field(default=None, max_length=64)
    status: str = Field(max_length=64)
    reason: str | None = Field(default=None, max_length=1_000)


class CapabilityGrantEventPayload(StrictModel):
    master_fencing_token: int = Field(ge=1)
    workflow_run_id: EntityId
    node_run_id: EntityId
    request_id: EntityId
    grant_id: EntityId
    target_task_id: EntityId
    action: str = Field(max_length=128)
    resource: str = Field(max_length=1_024)
    consumed_fencing_token: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=1_000)


class RecoveryEventPayload(StrictModel):
    master_fencing_token: int = Field(ge=1)
    workflow_run_id: EntityId | None = None
    session_id: EntityId
    action: str = Field(max_length=128)
    outcome: str = Field(max_length=128)
    reason: str = Field(max_length=1_000)


def register_runtime_events(registry: EventRegistry) -> None:
    registry.register(RUN_CREATED, WorkflowRunEventPayload)
    registry.register(RUN_STATE_CHANGED, WorkflowRunEventPayload)
    registry.register(NODE_STATE_CHANGED, NodeRunEventPayload)
    registry.register(TASK_STATE_CHANGED, TaskEventPayload)
    registry.register(PLANNER_STATE_CHANGED, PlannerRunEventPayload)
    registry.register(CHANGE_SET_STATE_CHANGED, ChangeSetEventPayload)
    registry.register(APPROVAL_STATE_CHANGED, ApprovalEventPayload)
    registry.register(PRIVILEGE_REQUEST_STATE_CHANGED, PrivilegeRequestEventPayload)
    registry.register(CAPABILITY_GRANT_STATE_CHANGED, CapabilityGrantEventPayload)
    registry.register(RECOVERY_ACTION, RecoveryEventPayload)


def build_runtime_event_registry() -> EventRegistry:
    registry = EventRegistry()
    register_runtime_events(registry)
    return registry


__all__ = [
    "APPROVAL_STATE_CHANGED",
    "CAPABILITY_GRANT_STATE_CHANGED",
    "CHANGE_SET_STATE_CHANGED",
    "NODE_STATE_CHANGED",
    "PLANNER_STATE_CHANGED",
    "PRIVILEGE_REQUEST_STATE_CHANGED",
    "RECOVERY_ACTION",
    "RUN_CREATED",
    "RUN_STATE_CHANGED",
    "TASK_STATE_CHANGED",
    "ApprovalEventPayload",
    "CapabilityGrantEventPayload",
    "ChangeSetEventPayload",
    "NodeRunEventPayload",
    "PlannerRunEventPayload",
    "PrivilegeRequestEventPayload",
    "RecoveryEventPayload",
    "TaskEventPayload",
    "WorkflowRunEventPayload",
    "build_runtime_event_registry",
    "register_runtime_events",
]
