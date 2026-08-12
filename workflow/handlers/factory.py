"""Default phase-one NodeRegistry construction."""

from __future__ import annotations

from protocol import NodeType
from workflow.handlers.agent_task import AgentTaskNodeHandler
from workflow.handlers.approval import ApprovalNodeHandler
from workflow.handlers.deterministic import (
    ContextBuilderNodeHandler,
    IfNodeHandler,
    InputNodeHandler,
    OutputNodeHandler,
    UnavailableWriteNodeHandler,
)
from workflow.handlers.guards import (
    CommandGuardNodeHandler,
    PatchGuardNodeHandler,
    RiskClassifierNodeHandler,
    TestNodeHandler,
)
from workflow.registry import NODE_CONFIG_MODELS, NodeRegistry


def build_node_registry(
    agent_task_handler: AgentTaskNodeHandler,
    *,
    patch_guard_handler: PatchGuardNodeHandler | None = None,
    command_guard_handler: CommandGuardNodeHandler | None = None,
    test_handler: TestNodeHandler | None = None,
    risk_handler: RiskClassifierNodeHandler | None = None,
    approval_handler: ApprovalNodeHandler | None = None,
    merge_patch_handler: object | None = None,
) -> NodeRegistry:
    unavailable = UnavailableWriteNodeHandler()
    handlers = {
        NodeType.INPUT: InputNodeHandler(),
        NodeType.AGENT_TASK: agent_task_handler,
        NodeType.CONTEXT_BUILDER: ContextBuilderNodeHandler(),
        NodeType.PATCH_GUARD: patch_guard_handler or unavailable,
        NodeType.COMMAND_GUARD: command_guard_handler or unavailable,
        NodeType.TEST: test_handler or unavailable,
        NodeType.RISK_CLASSIFIER: risk_handler or unavailable,
        NodeType.APPROVAL: approval_handler or unavailable,
        NodeType.MERGE_PATCH: merge_patch_handler or unavailable,
        NodeType.IF: IfNodeHandler(),
        NodeType.OUTPUT: OutputNodeHandler(),
    }
    registry = NodeRegistry()
    for node_type in NodeType:
        registry.register(node_type, NODE_CONFIG_MODELS[node_type], handlers[node_type])
    return registry


__all__ = ["build_node_registry"]
