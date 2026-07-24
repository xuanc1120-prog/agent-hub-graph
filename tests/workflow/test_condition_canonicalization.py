from __future__ import annotations

from master.router import AgentCatalog
from protocol import (
    AuthorGraph,
    EdgeCondition,
    IfCondition,
    IfOperator,
    NodeRunStatus,
    NodeType,
    TaskKind,
    WorkflowEdge,
    WorkflowNode,
)
from workflow.compiler import WorkflowCompiler
from workflow.graph_model import normalize_author_graph

BASE_COMMIT = "a" * 40


def _graph(values: list[str]) -> AuthorGraph:
    return AuthorGraph(
        nodes=[
            WorkflowNode(id="input", node_type=NodeType.INPUT, title="Input"),
            WorkflowNode(
                id="analyze",
                node_type=NodeType.AGENT_TASK,
                task_kind=TaskKind.ANALYZE,
                title="Analyze",
                instruction="Analyze read-only metadata.",
            ),
            WorkflowNode(
                id="branch",
                node_type=NodeType.IF,
                title="Branch",
                if_condition=IfCondition(
                    upstream_node_id="analyze",
                    field="status",
                    operator=IfOperator.IN,
                    value=values,
                ),
            ),
            WorkflowNode(id="output", node_type=NodeType.OUTPUT, title="Output"),
        ],
        edges=[
            WorkflowEdge(id="e1", from_node="input", to_node="analyze"),
            WorkflowEdge(id="e2", from_node="analyze", to_node="branch"),
            WorkflowEdge(
                id="e3",
                from_node="branch",
                to_node="output",
                condition=EdgeCondition.MATCHED,
            ),
            WorkflowEdge(
                id="e4",
                from_node="branch",
                to_node="output",
                condition=EdgeCondition.NOT_MATCHED,
            ),
        ],
    )


def test_in_operands_are_sorted_and_deduplicated() -> None:
    completed = NodeRunStatus.COMPLETED.value
    failed = NodeRunStatus.FAILED.value

    normalized = normalize_author_graph(_graph([failed, completed, failed]))
    branch = next(node for node in normalized.nodes if node.id == "branch")

    assert branch.if_condition is not None
    assert branch.if_condition.value == [completed, failed]


def test_equivalent_in_operands_have_identical_author_and_compiled_hashes(
    mock_catalog: AgentCatalog,
) -> None:
    completed = NodeRunStatus.COMPLETED.value
    failed = NodeRunStatus.FAILED.value
    compiler = WorkflowCompiler(mock_catalog)

    first = compiler.compile(
        _graph([completed, failed]),
        integration_base_commit=BASE_COMMIT,
    )
    second = compiler.compile(
        _graph([failed, completed, failed]),
        integration_base_commit=BASE_COMMIT,
    )

    assert first.ok and second.ok
    assert first.source_author_hash == second.source_author_hash
    assert first.compiled_hash == second.compiled_hash
    assert first.graph == second.graph
