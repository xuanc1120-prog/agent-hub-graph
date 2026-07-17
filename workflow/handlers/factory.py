"""Default phase-one NodeRegistry construction."""

from __future__ import annotations

from protocol import NodeType
from workflow.handlers.agent_task import AgentTaskNodeHandler
from workflow.handlers.deterministic import (
    ContextBuilderNodeHandler,
    IfNodeHandler,
    InputNodeHandler,
    OutputNodeHandler,
    UnavailableWriteNodeHandler,
)
from workflow.registry import NODE_CONFIG_MODELS, NodeRegistry


def build_node_registry(agent_task_handler: AgentTaskNodeHandler) -> NodeRegistry:
    handlers = {
        NodeType.INPUT: InputNodeHandler(),
        NodeType.AGENT_TASK: agent_task_handler,
        NodeType.CONTEXT_BUILDER: ContextBuilderNodeHandler(),
        NodeType.PATCH_GUARD: UnavailableWriteNodeHandler(),
        NodeType.COMMAND_GUARD: UnavailableWriteNodeHandler(),
        NodeType.TEST: UnavailableWriteNodeHandler(),
        NodeType.RISK_CLASSIFIER: UnavailableWriteNodeHandler(),
        NodeType.APPROVAL: UnavailableWriteNodeHandler(),
        NodeType.MERGE_PATCH: UnavailableWriteNodeHandler(),
        NodeType.IF: IfNodeHandler(),
        NodeType.OUTPUT: OutputNodeHandler(),
    }
    registry = NodeRegistry()
    for node_type in NodeType:
        registry.register(node_type, NODE_CONFIG_MODELS[node_type], handlers[node_type])
    return registry


__all__ = ["build_node_registry"]
