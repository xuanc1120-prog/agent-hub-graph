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


def normalize_command_templates(commands: list[list[str]]) -> list[list[str]]:
    """Deduplicate argv templates without changing execution order."""

    seen: set[tuple[str, ...]] = set()
    result: list[list[str]] = []
    for command in commands:
        key = tuple(command)
        if key not in seen:
            seen.add(key)
            result.append(list(command))
    return result


def normalize_author_graph(graph: AuthorGraph) -> AuthorGraph:
    """Canonicalize set-like fields while preserving ordered semantics."""

    nodes: list[WorkflowNode] = []
    for node in graph.nodes:
        nodes.append(
            node.model_copy(
                update={
                    "recommended_agents": sorted(
                        node.recommended_agents,
                        key=lambda item: (item.agent_id, -item.score, item.reason),
                    ),
                    "allowed_files_candidate": sorted(node.allowed_files_candidate),
                    "new_files_candidate": sorted(node.new_files_candidate),
                    # Command order is execution order. Preserve it, including
                    # duplicates, so DraftValidator can reject ambiguous input.
                    "allowed_commands_candidate": [
                        list(command) for command in node.allowed_commands_candidate
                    ],
                },
                deep=True,
            )
        )
    return AuthorGraph(
        schema_version=graph.schema_version,
        nodes=sorted(nodes, key=lambda item: item.id),
        edges=sorted(graph.edges, key=lambda item: item.id),
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
    "normalize_author_graph",
    "normalize_command_templates",
]
