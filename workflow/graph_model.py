"""Stable import location for the frozen workflow graph models.

The graph contract itself lives in :mod:`protocol.workflow` (HUB-010). This
module re-exports it so runtime workflow code (compiler, validators, executor)
can import graph models from ``workflow.graph_model`` as laid out in the project
directory structure, without depending on the ``protocol`` package layout.
"""

from __future__ import annotations

from protocol.workflow import (
    AgentRecommendation,
    AuthorGraph,
    CompiledGraph,
    IfCondition,
    NodeLayout,
    NodePosition,
    WorkflowDraft,
    WorkflowEdge,
    WorkflowLayout,
    WorkflowNode,
)

__all__ = [
    "AgentRecommendation",
    "AuthorGraph",
    "CompiledGraph",
    "IfCondition",
    "NodeLayout",
    "NodePosition",
    "WorkflowDraft",
    "WorkflowEdge",
    "WorkflowLayout",
    "WorkflowNode",
]
