from __future__ import annotations

from protocol import (
    AuthorGraph,
    EdgeCondition,
    IfCondition,
    IfOperator,
    NodeType,
    TaskKind,
    WorkflowEdge,
    WorkflowNode,
)
from workflow.validation import DraftValidator


def test_valid_readonly_graph_passes(readonly_graph: AuthorGraph) -> None:
    assert DraftValidator().validate(readonly_graph).ok


def test_rejects_compiler_owned_fields(readonly_graph: AuthorGraph) -> None:
    graph = readonly_graph.model_copy(deep=True)
    graph.nodes[1].effective_allowed_files = ["src/example.py"]

    report = DraftValidator().validate(graph)

    assert "compiler_only_field" in {issue.code for issue in report.errors}


def test_rejects_path_escape_wildcard_and_overlap(readonly_graph: AuthorGraph) -> None:
    graph = readonly_graph.model_copy(deep=True)
    graph.nodes[1].allowed_files_candidate = ["../secret", "src/*.py", "src/same.py"]
    graph.nodes[1].new_files_candidate = ["src/same.py"]

    report = DraftValidator().validate(graph)
    codes = {issue.code for issue in report.errors}

    assert "invalid_candidate_path" in codes
    assert "candidate_scope_overlap" in codes


def test_rejects_cycle_and_unknown_reference(readonly_graph: AuthorGraph) -> None:
    graph = readonly_graph.model_copy(deep=True)
    graph.edges.append(WorkflowEdge(id="cycle", from_node="output", to_node="input"))
    graph.edges.append(WorkflowEdge(id="missing", from_node="analyze", to_node="absent"))

    codes = {issue.code for issue in DraftValidator().validate(graph).errors}

    assert "graph_cycle" in codes
    assert "unknown_edge_target" in codes


def test_if_requires_matched_and_not_matched_edges() -> None:
    graph = AuthorGraph(
        nodes=[
            WorkflowNode(id="input", node_type=NodeType.INPUT, title="Input"),
            WorkflowNode(
                id="task",
                node_type=NodeType.AGENT_TASK,
                task_kind=TaskKind.ANALYZE,
                title="Task",
                instruction="Analyze.",
            ),
            WorkflowNode(id="branch", node_type=NodeType.IF, title="Branch"),
            WorkflowNode(id="output", node_type=NodeType.OUTPUT, title="Output"),
        ],
        edges=[
            WorkflowEdge(id="e1", from_node="input", to_node="task"),
            WorkflowEdge(id="e2", from_node="task", to_node="branch"),
            WorkflowEdge(id="e3", from_node="branch", to_node="output"),
        ],
    )

    codes = {issue.code for issue in DraftValidator().validate(graph).errors}

    assert "if_condition_missing" in codes
    assert "invalid_edge_condition" in codes
    assert "if_branch_missing" in codes


def test_if_may_reference_a_dominating_transitive_predecessor() -> None:
    graph = _transitive_if_graph()

    assert DraftValidator().validate(graph).ok


def test_if_rejects_a_predecessor_that_does_not_dominate_all_paths() -> None:
    graph = _transitive_if_graph()
    graph.nodes.insert(
        2,
        WorkflowNode(id="alternate", node_type=NodeType.CONTEXT_BUILDER, title="Alternate"),
    )
    graph.edges.extend(
        [
            WorkflowEdge(id="alt-1", from_node="input", to_node="alternate"),
            WorkflowEdge(id="alt-2", from_node="alternate", to_node="branch"),
        ]
    )

    codes = {issue.code for issue in DraftValidator().validate(graph).errors}

    assert "if_upstream_not_dominating" in codes


def test_if_rejects_unknown_enum_value_and_invalid_boolean_operator() -> None:
    graph = _transitive_if_graph()
    branch = next(node for node in graph.nodes if node.id == "branch")
    branch.if_condition = IfCondition(
        upstream_node_id="analyze",
        field="status",
        operator=IfOperator.EQ,
        value="not-a-node-status",
    )
    unknown_codes = {issue.code for issue in DraftValidator().validate(graph).errors}
    branch.if_condition = IfCondition(
        upstream_node_id="analyze",
        field="status",
        operator=IfOperator.IS_TRUE,
        value=None,
    )
    boolean_codes = {issue.code for issue in DraftValidator().validate(graph).errors}

    assert "if_operand_unknown" in unknown_codes
    assert "if_operand_invalid" in boolean_codes


def _transitive_if_graph() -> AuthorGraph:
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
                id="context",
                node_type=NodeType.CONTEXT_BUILDER,
                title="Build context",
            ),
            WorkflowNode(
                id="branch",
                node_type=NodeType.IF,
                title="Branch",
                if_condition=IfCondition(
                    upstream_node_id="analyze",
                    field="outcome",
                    operator=IfOperator.EQ,
                    value="success",
                ),
            ),
            WorkflowNode(id="output", node_type=NodeType.OUTPUT, title="Output"),
        ],
        edges=[
            WorkflowEdge(id="e1", from_node="input", to_node="analyze"),
            WorkflowEdge(id="e2", from_node="analyze", to_node="context"),
            WorkflowEdge(id="e3", from_node="context", to_node="branch"),
            WorkflowEdge(
                id="e4",
                from_node="branch",
                to_node="output",
                condition=EdgeCondition.MATCHED,
            ),
            WorkflowEdge(
                id="e5",
                from_node="branch",
                to_node="output",
                condition=EdgeCondition.NOT_MATCHED,
            ),
        ],
    )
