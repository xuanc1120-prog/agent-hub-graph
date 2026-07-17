"""Typed durable scheduler event payloads."""

from __future__ import annotations

from pydantic import Field

from protocol import (
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


class WorkflowRunEventPayload(StrictModel):
    master_fencing_token: int = Field(ge=1)
    workflow_run_id: EntityId
    previous_status: WorkflowRunStatus | None = None
    status: WorkflowRunStatus
    compiled_snapshot_hash: Sha256Hex | None = None
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


def register_runtime_events(registry: EventRegistry) -> None:
    registry.register(RUN_CREATED, WorkflowRunEventPayload)
    registry.register(RUN_STATE_CHANGED, WorkflowRunEventPayload)
    registry.register(NODE_STATE_CHANGED, NodeRunEventPayload)
    registry.register(TASK_STATE_CHANGED, TaskEventPayload)
    registry.register(PLANNER_STATE_CHANGED, PlannerRunEventPayload)


def build_runtime_event_registry() -> EventRegistry:
    registry = EventRegistry()
    register_runtime_events(registry)
    return registry


__all__ = [
    "NODE_STATE_CHANGED",
    "PLANNER_STATE_CHANGED",
    "RUN_CREATED",
    "RUN_STATE_CHANGED",
    "TASK_STATE_CHANGED",
    "NodeRunEventPayload",
    "PlannerRunEventPayload",
    "TaskEventPayload",
    "WorkflowRunEventPayload",
    "build_runtime_event_registry",
    "register_runtime_events",
]
