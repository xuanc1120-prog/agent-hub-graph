"""No-side-effect Master-owned node handlers."""

from __future__ import annotations

from protocol import IfOperator, NodeOutcome, NodeRunStatus, NodeType
from workflow.handlers.base import NodeExecutionContext, NodeHandlerResult


class InputNodeHandler:
    async def execute(self, context: object) -> NodeHandlerResult:
        resolved = _context(context)
        return NodeHandlerResult(
            status=NodeRunStatus.COMPLETED,
            outcome=NodeOutcome.SUCCESS,
            summary=f"Accepted session goal for {resolved.run.workflow_run_id}.",
        )


class ContextBuilderNodeHandler:
    async def execute(self, context: object) -> NodeHandlerResult:
        resolved = _context(context)
        return NodeHandlerResult(
            status=NodeRunStatus.COMPLETED,
            outcome=NodeOutcome.SUCCESS,
            summary=f"Prepared bounded context for node {resolved.node.id}.",
        )


class IfNodeHandler:
    async def execute(self, context: object) -> NodeHandlerResult:
        resolved = _context(context)
        condition = resolved.node.if_condition
        if condition is None:
            return _failed("if_condition_missing", "If node has no structured condition.")
        upstream = resolved.condition_upstream
        if upstream is None:
            return _failed("if_upstream_missing", "If condition upstream result is unavailable.")
        if condition.field == "status":
            actual: object = upstream.status.value
        elif condition.field == "outcome":
            actual = upstream.outcome.value if upstream.outcome is not None else None
        elif condition.field == "effective_risk":
            upstream_node = next(
                (
                    item
                    for item in resolved.run.compiled_snapshot.nodes
                    if item.id == condition.upstream_node_id
                ),
                None,
            )
            if upstream_node is None or upstream_node.policy_risk_floor is None:
                return _failed(
                    "if_field_unavailable",
                    "The referenced node has no frozen effective risk.",
                )
            actual = upstream_node.policy_risk_floor.value
        elif condition.field == "tests_passed":
            if upstream.node_type != NodeType.TEST:
                return _failed(
                    "if_field_unavailable",
                    "tests_passed is only available for a test node.",
                )
            actual = (
                upstream.status == NodeRunStatus.COMPLETED
                and upstream.outcome == NodeOutcome.SUCCESS
            )
        else:
            return _failed(
                "if_field_unavailable",
                f"If field {condition.field} is not available in HUB-110.",
            )
        try:
            matched = _evaluate(actual, condition.operator, condition.value)
        except (TypeError, ValueError):
            return _failed("if_evaluation_failed", "Structured condition could not be evaluated.")
        outcome = NodeOutcome.MATCHED if matched else NodeOutcome.NOT_MATCHED
        return NodeHandlerResult(
            status=NodeRunStatus.COMPLETED,
            outcome=outcome,
            summary=f"Condition evaluated to {outcome.value}.",
        )


class OutputNodeHandler:
    async def execute(self, context: object) -> NodeHandlerResult:
        resolved = _context(context)
        outcomes = {item.outcome for item in resolved.active_predecessors}
        if NodeOutcome.BLOCKED in outcomes:
            return NodeHandlerResult(
                status=NodeRunStatus.BLOCKED_BY_GUARD,
                outcome=NodeOutcome.BLOCKED,
                summary="Workflow reached output through a blocked branch.",
                error_code="upstream_blocked",
            )
        if NodeOutcome.FAILURE in outcomes:
            return _failed("upstream_failed", "Workflow reached output through a failure branch.")
        if NodeOutcome.REJECTED in outcomes:
            return NodeHandlerResult(
                status=NodeRunStatus.COMPLETED,
                outcome=NodeOutcome.REJECTED,
                summary="Workflow ended after explicit rejection; no change was merged.",
            )
        return NodeHandlerResult(
            status=NodeRunStatus.COMPLETED,
            outcome=NodeOutcome.SUCCESS,
            summary="Workflow completed successfully with no repository side effects.",
        )


class UnavailableWriteNodeHandler:
    async def execute(self, context: object) -> NodeHandlerResult:
        resolved = _context(context)
        return NodeHandlerResult(
            status=NodeRunStatus.BLOCKED_BY_GUARD,
            outcome=NodeOutcome.BLOCKED,
            summary=f"{resolved.node.node_type.value} requires the HUB-200/210 runtime.",
            error_code="write_runtime_unavailable",
        )


def _context(value: object) -> NodeExecutionContext:
    if not isinstance(value, NodeExecutionContext):
        raise TypeError("handler requires NodeExecutionContext")
    return value


def _failed(code: str, summary: str) -> NodeHandlerResult:
    return NodeHandlerResult(
        status=NodeRunStatus.FAILED,
        outcome=NodeOutcome.FAILURE,
        summary=summary,
        error_code=code,
    )


def _evaluate(actual: object, operator: IfOperator, expected: object) -> bool:
    if operator == IfOperator.EQ:
        return actual == expected
    if operator == IfOperator.NE:
        return actual != expected
    if operator == IfOperator.IN:
        if not isinstance(expected, list):
            raise TypeError("in operator requires a list")
        return actual in expected
    if operator == IfOperator.IS_TRUE:
        if not isinstance(actual, bool):
            raise TypeError("is_true requires a bool")
        return actual is True
    if operator == IfOperator.IS_FALSE:
        if not isinstance(actual, bool):
            raise TypeError("is_false requires a bool")
        return actual is False
    raise ValueError(f"unsupported operator: {operator}")


__all__ = [
    "ContextBuilderNodeHandler",
    "IfNodeHandler",
    "InputNodeHandler",
    "OutputNodeHandler",
    "UnavailableWriteNodeHandler",
]
