"""Structural validation for author and compiled workflow graphs."""

from __future__ import annotations

import re
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import PurePosixPath

from protocol import (
    AssignmentMode,
    AuthorGraph,
    EdgeCondition,
    IfOperator,
    NodeOutcome,
    NodeRunStatus,
    NodeType,
    RiskLevel,
    ValidationIssue,
    WorkflowEdge,
    WorkflowNode,
    canonical_json,
)

MAX_AUTHOR_GRAPH_BYTES = 2 * 1024 * 1024
MAX_AUTHOR_NODES = 100
MAX_AUTHOR_EDGES = 300

AUTHOR_NODE_TYPES = frozenset(
    {
        NodeType.INPUT,
        NodeType.AGENT_TASK,
        NodeType.CONTEXT_BUILDER,
        NodeType.IF,
        NodeType.OUTPUT,
    }
)
SYSTEM_NODE_TYPES = frozenset(NodeType).difference(AUTHOR_NODE_TYPES)

_DRIVE_PATH = re.compile(r"^[A-Za-z]:")
_WILDCARD_CHARS = frozenset("*?[]{}")
_COMPILER_ONLY_FIELDS = (
    "resolved_agent_id",
    "resolved_agent_spec_sha256",
    "effective_allowed_files",
    "effective_new_files",
    "effective_allowed_commands",
    "policy_risk_floor",
    "requires_changeset_approval",
    "test_kind",
    "test_argv",
    "source_node_id",
    "system_rule_id",
)


@dataclass(frozen=True, slots=True)
class ValidationReport:
    errors: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


class DraftValidator:
    """Validate untrusted AuthorGraph structure without granting permissions."""

    def validate(
        self,
        graph: AuthorGraph,
        *,
        require_complete: bool = True,
    ) -> ValidationReport:
        errors: list[ValidationIssue] = []
        warnings: list[ValidationIssue] = []

        if len(graph.nodes) > MAX_AUTHOR_NODES:
            errors.append(_issue("graph_too_many_nodes", "author graph exceeds 100 nodes"))
        if len(graph.edges) > MAX_AUTHOR_EDGES:
            errors.append(_issue("graph_too_many_edges", "author graph exceeds 300 edges"))
        if len(canonical_json(graph)) > MAX_AUTHOR_GRAPH_BYTES:
            errors.append(_issue("graph_too_large", "author graph canonical JSON exceeds 2 MiB"))

        node_counts = Counter(node.id for node in graph.nodes)
        edge_counts = Counter(edge.id for edge in graph.edges)
        for node_id, count in sorted(node_counts.items()):
            if count > 1:
                errors.append(
                    _issue("duplicate_node_id", f"duplicate node id: {node_id}", node_id=node_id)
                )
        for edge_id, count in sorted(edge_counts.items()):
            if count > 1:
                errors.append(
                    _issue("duplicate_edge_id", f"duplicate edge id: {edge_id}", edge_id=edge_id)
                )

        nodes = {node.id: node for node in graph.nodes}
        for node in sorted(graph.nodes, key=lambda item: item.id):
            errors.extend(self._validate_node(node))

        valid_edges: list[WorkflowEdge] = []
        for edge in sorted(graph.edges, key=lambda item: item.id):
            if edge.from_node not in nodes:
                errors.append(
                    _issue(
                        "unknown_edge_source",
                        f"edge source does not exist: {edge.from_node}",
                        edge_id=edge.id,
                    )
                )
            if edge.to_node not in nodes:
                errors.append(
                    _issue(
                        "unknown_edge_target",
                        f"edge target does not exist: {edge.to_node}",
                        edge_id=edge.id,
                    )
                )
            if edge.from_node == edge.to_node:
                errors.append(_issue("self_edge", "self edges are not allowed", edge_id=edge.id))
            if edge.system_managed:
                errors.append(
                    _issue(
                        "author_system_edge",
                        "AuthorGraph cannot contain system-managed edges",
                        edge_id=edge.id,
                    )
                )
            if edge.from_node in nodes and edge.to_node in nodes:
                valid_edges.append(edge)
                errors.extend(self._validate_edge_condition(edge, nodes[edge.from_node]))

        for node in graph.nodes:
            if node.node_type != NodeType.IF or node.if_condition is None:
                continue
            errors.extend(_validate_if_condition(node, nodes, valid_edges))

        errors.extend(self._validate_if_branches(graph.nodes, valid_edges))
        if len(nodes) == len(graph.nodes) and _contains_cycle(nodes, valid_edges):
            errors.append(_issue("graph_cycle", "Demo workflows must be acyclic"))

        if require_complete:
            errors.extend(self._validate_complete_graph(graph.nodes, valid_edges, nodes))
        elif not graph.nodes:
            warnings.append(_issue("draft_empty", "draft has no nodes"))

        return ValidationReport(tuple(errors), tuple(warnings))

    @staticmethod
    def _validate_node(node: WorkflowNode) -> list[ValidationIssue]:
        errors: list[ValidationIssue] = []
        if node.node_type not in AUTHOR_NODE_TYPES:
            errors.append(
                _issue(
                    "author_system_node_type",
                    f"{node.node_type.value} is compiler-only",
                    node_id=node.id,
                )
            )
        if node.system_managed:
            errors.append(
                _issue(
                    "author_system_node",
                    "AuthorGraph nodes cannot be system-managed",
                    node_id=node.id,
                )
            )
        for field_name in _COMPILER_ONLY_FIELDS:
            if getattr(node, field_name) is not None:
                errors.append(
                    _issue(
                        "compiler_only_field",
                        f"{field_name} can only be set by WorkflowCompiler",
                        node_id=node.id,
                    )
                )

        if node.node_type == NodeType.AGENT_TASK:
            if node.task_kind is None:
                errors.append(
                    _issue(
                        "agent_task_kind_missing",
                        "agent_task requires task_kind",
                        node_id=node.id,
                    )
                )
            if node.instruction is None:
                errors.append(
                    _issue(
                        "agent_instruction_missing",
                        "agent_task requires instruction",
                        node_id=node.id,
                    )
                )
            if node.assignment_mode == AssignmentMode.AUTO and node.assigned_agent is not None:
                errors.append(
                    _issue(
                        "auto_assignment_has_agent",
                        "auto assignment cannot pin assigned_agent",
                        node_id=node.id,
                    )
                )
            if (
                node.assignment_mode in {AssignmentMode.MANUAL, AssignmentMode.LOCKED}
                and node.assigned_agent is None
            ):
                errors.append(
                    _issue(
                        "explicit_assignment_missing_agent",
                        f"{node.assignment_mode.value} assignment requires assigned_agent",
                        node_id=node.id,
                    )
                )
            errors.extend(_validate_candidate_paths(node))
            errors.extend(_validate_candidate_commands(node))
            if not node.requires_write and (
                node.new_files_candidate or node.allowed_commands_candidate
            ):
                errors.append(
                    _issue(
                        "write_candidates_on_readonly_task",
                        "new files and command candidates require requires_write=true",
                        node_id=node.id,
                    )
                )
        else:
            if node.task_kind is not None:
                errors.append(
                    _issue(
                        "task_kind_on_non_agent",
                        "task_kind is only valid on agent_task",
                        node_id=node.id,
                    )
                )
            if node.assigned_agent is not None or node.recommended_agents:
                errors.append(
                    _issue(
                        "agent_assignment_on_system_node",
                        "Agent assignment is only valid on agent_task",
                        node_id=node.id,
                    )
                )
            if (
                node.allowed_files_candidate
                or node.new_files_candidate
                or node.allowed_commands_candidate
                or node.requires_write
            ):
                errors.append(
                    _issue(
                        "scope_on_non_agent",
                        "candidate permissions are only valid on agent_task",
                        node_id=node.id,
                    )
                )

        if node.node_type == NodeType.IF:
            if node.if_condition is None:
                errors.append(
                    _issue("if_condition_missing", "if node requires if_condition", node_id=node.id)
                )
        elif node.if_condition is not None:
            errors.append(
                _issue(
                    "if_condition_on_non_if",
                    "if_condition is only valid on if nodes",
                    node_id=node.id,
                )
            )
        return errors

    @staticmethod
    def _validate_edge_condition(
        edge: WorkflowEdge,
        source: WorkflowNode,
    ) -> list[ValidationIssue]:
        if source.node_type == NodeType.IF:
            allowed = {
                EdgeCondition.MATCHED,
                EdgeCondition.NOT_MATCHED,
                EdgeCondition.FAILURE,
            }
        else:
            allowed = {EdgeCondition.SUCCESS, EdgeCondition.FAILURE}
        if edge.condition in allowed:
            return []
        return [
            _issue(
                "invalid_edge_condition",
                f"{source.node_type.value} cannot emit {edge.condition.value}",
                edge_id=edge.id,
                node_id=source.id,
            )
        ]

    @staticmethod
    def _validate_if_branches(
        nodes: list[WorkflowNode],
        edges: list[WorkflowEdge],
    ) -> list[ValidationIssue]:
        errors: list[ValidationIssue] = []
        outgoing: dict[str, list[WorkflowEdge]] = {}
        for edge in edges:
            outgoing.setdefault(edge.from_node, []).append(edge)
        for node in nodes:
            if node.node_type != NodeType.IF:
                continue
            counts = Counter(edge.condition for edge in outgoing.get(node.id, []))
            for required in (EdgeCondition.MATCHED, EdgeCondition.NOT_MATCHED):
                if counts[required] != 1:
                    errors.append(
                        _issue(
                            "if_branch_missing",
                            f"if node requires exactly one {required.value} edge",
                            node_id=node.id,
                        )
                    )
            if counts[EdgeCondition.FAILURE] > 1:
                errors.append(
                    _issue(
                        "if_failure_fanout",
                        "if node may have at most one failure edge",
                        node_id=node.id,
                    )
                )
        return errors

    @staticmethod
    def _validate_complete_graph(
        node_list: list[WorkflowNode],
        edges: list[WorkflowEdge],
        nodes: dict[str, WorkflowNode],
    ) -> list[ValidationIssue]:
        errors: list[ValidationIssue] = []
        inputs = [node for node in node_list if node.node_type == NodeType.INPUT]
        outputs = [node for node in node_list if node.node_type == NodeType.OUTPUT]
        if len(inputs) != 1:
            errors.append(_issue("input_count", "workflow requires exactly one input node"))
        if len(outputs) != 1:
            errors.append(_issue("output_count", "workflow requires exactly one output node"))

        incoming = Counter(edge.to_node for edge in edges)
        outgoing = Counter(edge.from_node for edge in edges)
        if len(inputs) == 1 and incoming[inputs[0].id] != 0:
            errors.append(
                _issue("input_has_incoming", "input node cannot have incoming edges", inputs[0].id)
            )
        if len(outputs) == 1 and outgoing[outputs[0].id] != 0:
            errors.append(
                _issue(
                    "output_has_outgoing", "output node cannot have outgoing edges", outputs[0].id
                )
            )
        for node in node_list:
            if node.node_type != NodeType.OUTPUT and outgoing[node.id] == 0:
                errors.append(
                    _issue(
                        "dead_end_node",
                        "every non-output node requires an outgoing edge",
                        node.id,
                    )
                )
            if len(node_list) > 1 and incoming[node.id] == 0 and outgoing[node.id] == 0:
                errors.append(
                    _issue(
                        "isolated_node", "complete workflow cannot contain isolated nodes", node.id
                    )
                )

        if len(inputs) == 1 and len(nodes) == len(node_list):
            reachable = _reachable_from(inputs[0].id, edges)
            for node_id in sorted(nodes.keys() - reachable):
                errors.append(
                    _issue(
                        "unreachable_node",
                        "node is not reachable from input",
                        node_id=node_id,
                    )
                )
        return errors


def _validate_candidate_paths(node: WorkflowNode) -> list[ValidationIssue]:
    errors: list[ValidationIssue] = []
    existing = list(node.allowed_files_candidate)
    new = list(node.new_files_candidate)
    for field_name, values in (
        ("allowed_files_candidate", existing),
        ("new_files_candidate", new),
    ):
        if len(values) != len(set(values)):
            errors.append(
                _issue(
                    "duplicate_candidate_path",
                    f"{field_name} contains duplicate paths",
                    node_id=node.id,
                )
            )
        for value in values:
            reason = _invalid_repo_path_reason(value)
            if reason is not None:
                errors.append(
                    _issue(
                        "invalid_candidate_path",
                        f"invalid {field_name} path {value!r}: {reason}",
                        node_id=node.id,
                    )
                )
    overlap = sorted(set(existing).intersection(new))
    if overlap:
        errors.append(
            _issue(
                "candidate_scope_overlap",
                f"existing and new candidate paths overlap: {overlap[0]}",
                node_id=node.id,
            )
        )
    return errors


def _validate_if_condition(
    node: WorkflowNode,
    nodes: dict[str, WorkflowNode],
    edges: list[WorkflowEdge],
) -> list[ValidationIssue]:
    condition = node.if_condition
    assert condition is not None
    errors: list[ValidationIssue] = []
    upstream_id = condition.upstream_node_id
    if upstream_id not in nodes:
        return [
            _issue(
                "if_upstream_unknown",
                "if condition references an unknown upstream node",
                node_id=node.id,
            )
        ]

    predecessors = _transitive_predecessors(node.id, edges)
    if upstream_id not in predecessors:
        errors.append(
            _issue(
                "if_upstream_not_predecessor",
                "if condition must reference a transitive predecessor",
                node_id=node.id,
            )
        )
    elif upstream_id not in _dominators(nodes, edges).get(node.id, set()):
        errors.append(
            _issue(
                "if_upstream_not_dominating",
                "if condition upstream must be completed on every path to the if node",
                node_id=node.id,
            )
        )

    expected_values: set[str] | None
    if condition.field == "status":
        expected_values = {value.value for value in NodeRunStatus}
    elif condition.field == "outcome":
        expected_values = {value.value for value in NodeOutcome}
    elif condition.field == "effective_risk":
        expected_values = {value.value for value in RiskLevel}
    else:
        expected_values = None

    value = condition.value
    if condition.operator in {IfOperator.IS_TRUE, IfOperator.IS_FALSE}:
        if condition.field != "tests_passed" or value is not None:
            errors.append(
                _issue(
                    "if_operand_invalid",
                    "is_true/is_false require tests_passed with no value",
                    node_id=node.id,
                )
            )
    elif condition.operator == IfOperator.IN:
        if condition.field == "tests_passed" or not isinstance(value, list):
            errors.append(
                _issue(
                    "if_operand_invalid",
                    "in requires a non-boolean field and a non-empty value list",
                    node_id=node.id,
                )
            )
        elif expected_values is not None and any(item not in expected_values for item in value):
            errors.append(
                _issue(
                    "if_operand_unknown",
                    f"if condition contains an unknown {condition.field} value",
                    node_id=node.id,
                )
            )
    elif condition.operator in {IfOperator.EQ, IfOperator.NE}:
        if condition.field == "tests_passed":
            valid_scalar = isinstance(value, bool)
        else:
            valid_scalar = isinstance(value, str) and expected_values is not None
        if not valid_scalar:
            errors.append(
                _issue(
                    "if_operand_invalid",
                    "eq/ne require a scalar value matching the selected field",
                    node_id=node.id,
                )
            )
        elif expected_values is not None and value not in expected_values:
            errors.append(
                _issue(
                    "if_operand_unknown",
                    f"if condition contains an unknown {condition.field} value",
                    node_id=node.id,
                )
            )
    return errors


def _invalid_repo_path_reason(value: str) -> str | None:
    if "\x00" in value or any(ord(char) < 32 for char in value):
        return "control characters are forbidden"
    if "\\" in value:
        return "use canonical forward slashes"
    if value.startswith("/") or _DRIVE_PATH.match(value):
        return "absolute paths are forbidden"
    if any(char in value for char in _WILDCARD_CHARS):
        return "wildcards are forbidden"
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        return "dot and parent segments are forbidden"
    if value.endswith("/"):
        return "file paths cannot end with a slash"
    if path.as_posix() != value:
        return "path is not canonical"
    return None


def _validate_candidate_commands(node: WorkflowNode) -> list[ValidationIssue]:
    errors: list[ValidationIssue] = []
    seen: set[tuple[str, ...]] = set()
    for argv in node.allowed_commands_candidate:
        normalized = tuple(argv)
        if normalized in seen:
            errors.append(
                _issue(
                    "duplicate_command_candidate",
                    "allowed command candidates must be unique",
                    node_id=node.id,
                )
            )
        seen.add(normalized)
        for token in argv:
            if "\x00" in token or "\r" in token or "\n" in token:
                errors.append(
                    _issue(
                        "invalid_command_token",
                        "command argv tokens cannot contain NUL or newlines",
                        node_id=node.id,
                    )
                )
                break
    return errors


def _transitive_predecessors(target: str, edges: list[WorkflowEdge]) -> set[str]:
    incoming: dict[str, list[str]] = {}
    for edge in edges:
        incoming.setdefault(edge.to_node, []).append(edge.from_node)
    result: set[str] = set()
    queue = deque(incoming.get(target, []))
    while queue:
        current = queue.popleft()
        if current in result:
            continue
        result.add(current)
        queue.extend(incoming.get(current, []))
    return result


def _dominators(
    nodes: dict[str, WorkflowNode],
    edges: list[WorkflowEdge],
) -> dict[str, set[str]]:
    """Return graph dominators without assuming node order or one valid root."""

    incoming: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    for edge in edges:
        incoming[edge.to_node].add(edge.from_node)
    all_nodes = set(nodes)
    dominators = {
        node_id: ({node_id} if not predecessors else set(all_nodes))
        for node_id, predecessors in incoming.items()
    }
    for _ in range(max(1, len(nodes))):
        changed = False
        for node_id in sorted(nodes):
            predecessors = incoming[node_id]
            if not predecessors:
                updated = {node_id}
            else:
                shared = set.intersection(*(dominators[item] for item in predecessors))
                updated = {node_id, *shared}
            if updated != dominators[node_id]:
                dominators[node_id] = updated
                changed = True
        if not changed:
            break
    return dominators


def _contains_cycle(nodes: dict[str, WorkflowNode], edges: list[WorkflowEdge]) -> bool:
    indegree = {node_id: 0 for node_id in nodes}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for edge in edges:
        indegree[edge.to_node] += 1
        outgoing[edge.from_node].append(edge.to_node)
    queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for target in sorted(outgoing[current]):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    return visited != len(nodes)


def _reachable_from(start: str, edges: list[WorkflowEdge]) -> set[str]:
    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        outgoing.setdefault(edge.from_node, []).append(edge.to_node)
    reachable = {start}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for target in outgoing.get(current, []):
            if target not in reachable:
                reachable.add(target)
                queue.append(target)
    return reachable


def _issue(
    code: str,
    message: str,
    node_id: str | None = None,
    edge_id: str | None = None,
) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, node_id=node_id, edge_id=edge_id)


__all__ = [
    "AUTHOR_NODE_TYPES",
    "MAX_AUTHOR_EDGES",
    "MAX_AUTHOR_GRAPH_BYTES",
    "MAX_AUTHOR_NODES",
    "SYSTEM_NODE_TYPES",
    "DraftValidator",
    "ValidationReport",
]
