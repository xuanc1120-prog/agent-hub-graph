"""Typed NodeHandler execution boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from pydantic import Field, model_validator

from protocol import (
    ArtifactRef,
    FrozenStrictModel,
    NodeOutcome,
    NodeRunStatus,
    SummaryText,
    WorkflowNode,
)
from storage.leases import MasterLease
from storage.repositories import SessionRecord
from storage.workflow_run_repository import NodeRunRecord, WorkflowRunRecord


class NodeHandlerResult(FrozenStrictModel):
    status: NodeRunStatus
    outcome: NodeOutcome
    summary: SummaryText
    artifact_refs: tuple[ArtifactRef, ...] = Field(default_factory=tuple, max_length=500)
    error_code: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> Self:
        allowed = {
            NodeRunStatus.COMPLETED: {
                NodeOutcome.SUCCESS,
                NodeOutcome.MATCHED,
                NodeOutcome.NOT_MATCHED,
                NodeOutcome.APPROVED,
                NodeOutcome.REJECTED,
            },
            NodeRunStatus.FAILED: {NodeOutcome.FAILURE},
            NodeRunStatus.BLOCKED_BY_GUARD: {NodeOutcome.BLOCKED},
        }
        if self.status not in allowed or self.outcome not in allowed[self.status]:
            raise ValueError("handler status and outcome are inconsistent")
        if (
            self.status in {NodeRunStatus.FAILED, NodeRunStatus.BLOCKED_BY_GUARD}
            and not self.error_code
        ):
            raise ValueError("failed or blocked handler result requires error_code")
        return self


@dataclass(frozen=True, slots=True)
class NodeExecutionContext:
    run: WorkflowRunRecord
    node_run: NodeRunRecord
    node: WorkflowNode
    session: SessionRecord
    predecessors: tuple[NodeRunRecord, ...]
    active_predecessors: tuple[NodeRunRecord, ...]
    condition_upstream: NodeRunRecord | None
    master_lease: MasterLease


__all__ = ["NodeExecutionContext", "NodeHandlerResult"]
