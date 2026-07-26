"""One config model and one handler registration for every node type."""

from __future__ import annotations

import inspect
from typing import ClassVar, Protocol, TypeVar

from pydantic import model_validator

from protocol import FrozenStrictModel, NodeType, WorkflowNode


class NodeHandler(Protocol):
    async def execute(self, context: object) -> object: ...


class NodeConfig(FrozenStrictModel):
    expected_type: ClassVar[NodeType]
    node: WorkflowNode

    @model_validator(mode="after")
    def validate_node_type(self) -> NodeConfig:
        if self.node.node_type != self.expected_type:
            raise ValueError(
                f"expected {self.expected_type.value}, got {self.node.node_type.value}"
            )
        return self


class InputNodeConfig(NodeConfig):
    expected_type = NodeType.INPUT


class AgentTaskNodeConfig(NodeConfig):
    expected_type = NodeType.AGENT_TASK


class ContextBuilderNodeConfig(NodeConfig):
    expected_type = NodeType.CONTEXT_BUILDER


class PatchGuardNodeConfig(NodeConfig):
    expected_type = NodeType.PATCH_GUARD


class CommandGuardNodeConfig(NodeConfig):
    expected_type = NodeType.COMMAND_GUARD


class TestNodeConfig(NodeConfig):
    expected_type = NodeType.TEST


class RiskClassifierNodeConfig(NodeConfig):
    expected_type = NodeType.RISK_CLASSIFIER


class ApprovalNodeConfig(NodeConfig):
    expected_type = NodeType.APPROVAL


class MergePatchNodeConfig(NodeConfig):
    expected_type = NodeType.MERGE_PATCH


class IfNodeConfig(NodeConfig):
    expected_type = NodeType.IF


class OutputNodeConfig(NodeConfig):
    expected_type = NodeType.OUTPUT


ConfigT = TypeVar("ConfigT", bound=NodeConfig)


class NodeRegistry:
    """Reject duplicate or untyped node execution registrations."""

    def __init__(self) -> None:
        self._entries: dict[NodeType, tuple[type[NodeConfig], NodeHandler]] = {}

    def register(
        self,
        node_type: NodeType,
        config_model: type[ConfigT],
        handler: NodeHandler,
    ) -> None:
        if node_type in self._entries:
            raise ValueError(f"node type already registered: {node_type.value}")
        if not issubclass(config_model, NodeConfig):
            raise TypeError("config_model must inherit NodeConfig")
        if config_model.expected_type != node_type:
            raise ValueError("config model expected_type does not match registration")
        execute = getattr(handler, "execute", None)
        if not callable(execute) or not inspect.iscoroutinefunction(execute):
            raise TypeError("handler must provide async execute(context)")
        self._entries[node_type] = (config_model, handler)

    def is_registered(self, node_type: NodeType) -> bool:
        return node_type in self._entries

    def validate_config(self, node: WorkflowNode) -> NodeConfig:
        try:
            config_model, _handler = self._entries[node.node_type]
        except KeyError as exc:
            raise KeyError(f"no handler registered for {node.node_type.value}") from exc
        return config_model(node=node)

    def get_handler(self, node: WorkflowNode) -> NodeHandler:
        self.validate_config(node)
        return self._entries[node.node_type][1]

    @property
    def registered_types(self) -> frozenset[NodeType]:
        return frozenset(self._entries)


NODE_CONFIG_MODELS: dict[NodeType, type[NodeConfig]] = {
    NodeType.INPUT: InputNodeConfig,
    NodeType.AGENT_TASK: AgentTaskNodeConfig,
    NodeType.CONTEXT_BUILDER: ContextBuilderNodeConfig,
    NodeType.PATCH_GUARD: PatchGuardNodeConfig,
    NodeType.COMMAND_GUARD: CommandGuardNodeConfig,
    NodeType.TEST: TestNodeConfig,
    NodeType.RISK_CLASSIFIER: RiskClassifierNodeConfig,
    NodeType.APPROVAL: ApprovalNodeConfig,
    NodeType.MERGE_PATCH: MergePatchNodeConfig,
    NodeType.IF: IfNodeConfig,
    NodeType.OUTPUT: OutputNodeConfig,
}


__all__ = [
    "NODE_CONFIG_MODELS",
    "NodeConfig",
    "NodeHandler",
    "NodeRegistry",
]
