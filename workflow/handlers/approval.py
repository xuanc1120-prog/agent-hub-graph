"""Approval node handler for the durable ChangeSet approval gate."""

from __future__ import annotations

from protocol import ApprovalStatus, NodeOutcome, NodeRunStatus
from workflow.approval_manager import ApprovalManager
from workflow.handlers.base import NodeExecutionContext, NodeHandlerResult


class ApprovalNodeHandler:
    def __init__(self, manager: ApprovalManager) -> None:
        self._manager = manager

    async def execute(self, value: object) -> NodeHandlerResult:
        if not isinstance(value, NodeExecutionContext):
            raise TypeError("approval handler requires NodeExecutionContext")
        approval = await self._manager.execute(value)
        status = approval.approval.status
        if status == ApprovalStatus.APPROVED:
            return NodeHandlerResult(
                status=NodeRunStatus.COMPLETED,
                outcome=NodeOutcome.APPROVED,
                summary="ChangeSet approval was confirmed.",
            )
        if status == ApprovalStatus.REJECTED:
            return NodeHandlerResult(
                status=NodeRunStatus.COMPLETED,
                outcome=NodeOutcome.REJECTED,
                summary="ChangeSet approval was rejected.",
            )
        return NodeHandlerResult(
            status=NodeRunStatus.BLOCKED_BY_GUARD,
            outcome=NodeOutcome.BLOCKED,
            summary=f"ChangeSet approval is {status.value}.",
            error_code=f"approval_{status.value}",
        )


__all__ = ["ApprovalNodeHandler"]
