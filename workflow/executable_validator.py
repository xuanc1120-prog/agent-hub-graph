"""Execution-level validation for immutable CompiledGraph snapshots."""

from __future__ import annotations

from collections import Counter, deque
from pathlib import PurePosixPath

from protocol import (
    CompiledGraph,
    EdgeCondition,
    NodeType,
    TaskKind,
    TestKind,
    ValidationIssue,
    WorkflowEdge,
    WorkflowNode,
)
from workflow.registry import NodeRegistry
from workflow.validation import ValidationReport

MAX_COMPILED_NODES = 300
MAX_COMPILED_EDGES = 600
_SYSTEM_TYPES = frozenset(
    {
        NodeType.PATCH_GUARD,
        NodeType.COMMAND_GUARD,
        NodeType.TEST,
        NodeType.RISK_CLASSIFIER,
        NodeType.APPROVAL,
        NodeType.MERGE_PATCH,
    }
)
_DOC_EXTENSIONS = frozenset({".md", ".mdx", ".rst", ".txt"})
_SENSITIVE_DOC_STEMS = frozenset({"requirements", "constraints", "manifest", "config", "policy"})
_SENSITIVE_DOC_NAMES = frozenset({"package.json", "pyproject.toml"})


class ExecutableValidator:
    def __init__(self, registry: NodeRegistry, *, write_runtime_enabled: bool = False) -> None:
        self._registry = registry
        self._write_runtime_enabled = write_runtime_enabled

    def validate(self, graph: CompiledGraph) -> ValidationReport:
        errors: list[ValidationIssue] = []
        if len(graph.nodes) > MAX_COMPILED_NODES:
            errors.append(_issue("compiled_too_many_nodes", "compiled graph exceeds 300 nodes"))
        if len(graph.edges) > MAX_COMPILED_EDGES:
            errors.append(_issue("compiled_too_many_edges", "compiled graph exceeds 600 edges"))

        node_counts = Counter(node.id for node in graph.nodes)
        edge_counts = Counter(edge.id for edge in graph.edges)
        for node_id, count in sorted(node_counts.items()):
            if count > 1:
                errors.append(
                    _issue("duplicate_node_id", "compiled node id is not unique", node_id)
                )
        for edge_id, count in sorted(edge_counts.items()):
            if count > 1:
                errors.append(
                    ValidationIssue(
                        code="duplicate_edge_id",
                        message="compiled edge id is not unique",
                        edge_id=edge_id,
                    )
                )

        nodes = {node.id: node for node in graph.nodes}
        valid_edges: list[WorkflowEdge] = []
        for node in sorted(graph.nodes, key=lambda item: item.id):
            if not self._registry.is_registered(node.node_type):
                errors.append(
                    _issue(
                        "handler_unregistered",
                        f"no NodeHandler registered for {node.node_type.value}",
                        node.id,
                    )
                )
            else:
                try:
                    self._registry.validate_config(node)
                except (TypeError, ValueError) as exc:
                    errors.append(_issue("node_config_invalid", str(exc), node.id))
            errors.extend(self._validate_node(node, nodes))

        for edge in sorted(graph.edges, key=lambda item: item.id):
            if edge.from_node not in nodes or edge.to_node not in nodes:
                errors.append(
                    ValidationIssue(
                        code="compiled_edge_reference",
                        message="compiled edge references an unknown node",
                        edge_id=edge.id,
                    )
                )
                continue
            valid_edges.append(edge)
            errors.extend(_validate_condition(edge, nodes[edge.from_node]))
            source = nodes[edge.from_node]
            target = nodes[edge.to_node]
            touches_system = source.node_type in _SYSTEM_TYPES or target.node_type in _SYSTEM_TYPES
            if touches_system and not edge.system_managed:
                errors.append(
                    ValidationIssue(
                        code="unsealed_system_edge",
                        message="edges entering or leaving security nodes must be compiler-managed",
                        edge_id=edge.id,
                    )
                )
            if edge.system_managed and not touches_system:
                errors.append(
                    ValidationIssue(
                        code="unexpected_system_edge",
                        message="compiler-managed edge does not touch a system node",
                        edge_id=edge.id,
                    )
                )
            if (
                source.node_type in _SYSTEM_TYPES
                and target.node_type in _SYSTEM_TYPES
                and source.source_node_id != target.source_node_id
            ):
                errors.append(
                    ValidationIssue(
                        code="cross_task_security_edge",
                        message="security chain edges cannot cross source tasks",
                        edge_id=edge.id,
                    )
                )

        if len(nodes) == len(graph.nodes) and _contains_cycle(nodes, valid_edges):
            errors.append(_issue("compiled_cycle", "compiled graph must be a DAG"))
        errors.extend(_validate_boundaries(graph.nodes, valid_edges, nodes))
        errors.extend(_validate_fanout_and_joins(graph.nodes, valid_edges))
        errors.extend(_validate_outcome_branches(graph.nodes, valid_edges))
        errors.extend(_validate_security_chains(graph.nodes, valid_edges))

        if not self._write_runtime_enabled:
            for node in graph.nodes:
                if node.node_type == NodeType.AGENT_TASK and node.requires_write:
                    errors.append(
                        _issue(
                            "write_runtime_unavailable",
                            "write workflows require HUB-200 workspace and guard runtime",
                            node.id,
                        )
                    )
        return ValidationReport(errors=tuple(errors))

    @staticmethod
    def _validate_node(
        node: WorkflowNode,
        nodes: dict[str, WorkflowNode],
    ) -> list[ValidationIssue]:
        errors: list[ValidationIssue] = []
        if node.node_type == NodeType.AGENT_TASK:
            if node.task_kind is None:
                errors.append(
                    _issue("compiled_task_kind_missing", "agent_task needs task_kind", node.id)
                )
            if node.resolved_agent_id is None or node.resolved_agent_spec_sha256 is None:
                errors.append(
                    _issue(
                        "compiled_agent_unresolved",
                        "agent_task must carry frozen agent identity and spec hash",
                        node.id,
                    )
                )
            if (
                node.effective_allowed_files is None
                or node.effective_new_files is None
                or node.effective_allowed_commands is None
                or node.policy_risk_floor is None
                or node.requires_changeset_approval is None
            ):
                errors.append(
                    _issue(
                        "compiled_permissions_missing",
                        "agent_task is missing compiler-owned effective permissions",
                        node.id,
                    )
                )
            if node.requires_write and not (
                node.effective_allowed_files or node.effective_new_files
            ):
                errors.append(_issue("compiled_write_scope_empty", "write scope is empty", node.id))
            effective_files = set(node.effective_allowed_files or [])
            effective_new = set(node.effective_new_files or [])
            candidate_files = set(node.allowed_files_candidate)
            candidate_new = set(node.new_files_candidate)
            if not effective_files.issubset(candidate_files) or not effective_new.issubset(
                candidate_new
            ):
                errors.append(
                    _issue(
                        "compiled_scope_widened",
                        "effective file scope must be a subset of author candidates",
                        node.id,
                    )
                )
            if len(effective_files | effective_new) > 100:
                errors.append(
                    _issue(
                        "compiled_file_scope_limit",
                        "effective file scope cannot exceed 100 exact paths",
                        node.id,
                    )
                )
            candidate_commands = {tuple(command) for command in node.allowed_commands_candidate}
            effective_commands = {
                tuple(command) for command in (node.effective_allowed_commands or [])
            }
            if not effective_commands.issubset(candidate_commands):
                errors.append(
                    _issue(
                        "compiled_commands_widened",
                        "effective commands must be a subset of author candidates",
                        node.id,
                    )
                )
            if node.requires_write and node.requires_changeset_approval is not True:
                errors.append(
                    _issue(
                        "compiled_approval_missing",
                        "write agent_task must require final ChangeSet approval",
                        node.id,
                    )
                )
            if (
                node.system_managed
                or node.source_node_id is not None
                or node.system_rule_id is not None
            ):
                errors.append(
                    _issue(
                        "agent_task_marked_system",
                        "agent_task cannot carry compiler system-node ownership",
                        node.id,
                    )
                )
            if node.test_kind is not None or node.test_argv is not None:
                errors.append(
                    _issue(
                        "agent_task_test_config",
                        "test configuration is only valid on test nodes",
                        node.id,
                    )
                )
            if not node.requires_write and (
                node.effective_new_files
                or node.effective_allowed_commands
                or node.requires_changeset_approval
            ):
                errors.append(
                    _issue(
                        "readonly_effective_scope",
                        "read-only agent_task cannot carry write, command, "
                        "or approval capabilities",
                        node.id,
                    )
                )
        elif node.node_type in _SYSTEM_TYPES:
            if (
                not node.system_managed
                or node.source_node_id is None
                or node.system_rule_id is None
            ):
                errors.append(
                    _issue(
                        "system_node_unsealed",
                        "system node lacks compiler ownership metadata",
                        node.id,
                    )
                )
            elif (
                node.source_node_id not in nodes
                or nodes[node.source_node_id].node_type != NodeType.AGENT_TASK
                or not nodes[node.source_node_id].requires_write
            ):
                errors.append(
                    _issue(
                        "system_source_invalid",
                        "system node source must be a write agent_task",
                        node.id,
                    )
                )
            elif node.policy_risk_floor != nodes[node.source_node_id].policy_risk_floor:
                errors.append(
                    _issue(
                        "system_risk_mismatch",
                        "system node risk floor must match its source task",
                        node.id,
                    )
                )
            if node.node_type == NodeType.TEST:
                errors.extend(_validate_test_node(node, nodes))
            elif node.test_kind is not None or node.test_argv is not None:
                errors.append(
                    _issue(
                        "test_config_on_non_test",
                        "test_kind/test_argv are only valid on test nodes",
                        node.id,
                    )
                )
            if (
                node.task_kind is not None
                or node.assigned_agent is not None
                or node.resolved_agent_id is not None
                or node.resolved_agent_spec_sha256 is not None
                or node.recommended_agents
                or node.allowed_files_candidate
                or node.new_files_candidate
                or node.allowed_commands_candidate
                or node.effective_allowed_files is not None
                or node.effective_new_files is not None
                or node.effective_allowed_commands is not None
                or node.requires_changeset_approval is not None
                or node.requires_write
                or node.if_condition is not None
            ):
                errors.append(
                    _issue(
                        "system_node_field_leak",
                        "system node carries task, assignment, or permission fields",
                        node.id,
                    )
                )
        elif node.system_managed:
            errors.append(
                _issue(
                    "author_node_marked_system",
                    "author node types cannot be compiler-managed",
                    node.id,
                )
            )
        else:
            if (
                node.resolved_agent_id is not None
                or node.resolved_agent_spec_sha256 is not None
                or node.effective_allowed_files is not None
                or node.effective_new_files is not None
                or node.effective_allowed_commands is not None
                or node.policy_risk_floor is not None
                or node.requires_changeset_approval is not None
                or node.test_kind is not None
                or node.test_argv is not None
                or node.source_node_id is not None
                or node.system_rule_id is not None
            ):
                errors.append(
                    _issue(
                        "compiler_field_on_author_node",
                        "non-agent author node carries compiler-owned fields",
                        node.id,
                    )
                )
        return errors


def _validate_test_node(
    node: WorkflowNode,
    nodes: dict[str, WorkflowNode],
) -> list[ValidationIssue]:
    errors: list[ValidationIssue] = []
    if node.test_kind is None:
        return [_issue("test_kind_missing", "test node requires test_kind", node.id)]
    source = nodes.get(node.source_node_id or "")
    if node.test_kind == TestKind.COMMAND and not node.test_argv:
        errors.append(
            _issue("test_command_missing", "command test requires a non-empty argv", node.id)
        )
    if node.test_kind == TestKind.DOCS_STATIC:
        if node.test_argv is not None:
            errors.append(
                _issue("docs_static_has_argv", "docs_static test cannot execute argv", node.id)
            )
        if source is None or source.task_kind != TaskKind.DOCS:
            errors.append(
                _issue("docs_static_source", "docs_static requires a docs source task", node.id)
            )
        elif not _pure_docs_scope(source):
            errors.append(
                _issue(
                    "docs_static_scope", "docs_static requires a pure documentation scope", node.id
                )
            )
    return errors


def _validate_boundaries(
    node_list: list[WorkflowNode],
    edges: list[WorkflowEdge],
    nodes: dict[str, WorkflowNode],
) -> list[ValidationIssue]:
    errors: list[ValidationIssue] = []
    inputs = [node for node in node_list if node.node_type == NodeType.INPUT]
    outputs = [node for node in node_list if node.node_type == NodeType.OUTPUT]
    if len(inputs) != 1:
        errors.append(_issue("input_count", "compiled graph requires exactly one input"))
    if len(outputs) != 1:
        errors.append(_issue("output_count", "compiled graph requires exactly one output"))
    incoming = Counter(edge.to_node for edge in edges)
    outgoing = Counter(edge.from_node for edge in edges)
    if len(inputs) == 1 and incoming[inputs[0].id]:
        errors.append(
            _issue("input_has_incoming", "input cannot have incoming edges", inputs[0].id)
        )
    if len(outputs) == 1 and outgoing[outputs[0].id]:
        errors.append(
            _issue("output_has_outgoing", "output cannot have outgoing edges", outputs[0].id)
        )
    for node in node_list:
        if node.node_type != NodeType.OUTPUT and outgoing[node.id] == 0:
            errors.append(
                _issue(
                    "dead_end_node",
                    "every executable non-output node requires an outgoing edge",
                    node.id,
                )
            )
        if len(node_list) > 1 and incoming[node.id] == 0 and outgoing[node.id] == 0:
            errors.append(
                _issue("isolated_node", "compiled graph contains an isolated node", node.id)
            )
    if len(inputs) == 1:
        reachable = _reachable(inputs[0].id, edges)
        for node_id in sorted(nodes.keys() - reachable):
            errors.append(_issue("unreachable_node", "node is unreachable from input", node_id))
    if len(outputs) == 1:
        can_reach_output = _reverse_reachable(outputs[0].id, edges)
        for node_id in sorted(nodes.keys() - can_reach_output):
            errors.append(
                _issue("output_unreachable", "node cannot reach the output node", node_id)
            )
    return errors


def _validate_condition(edge: WorkflowEdge, source: WorkflowNode) -> list[ValidationIssue]:
    if source.node_type == NodeType.IF:
        allowed = {EdgeCondition.MATCHED, EdgeCondition.NOT_MATCHED, EdgeCondition.FAILURE}
    elif source.node_type == NodeType.APPROVAL:
        allowed = {EdgeCondition.APPROVED, EdgeCondition.REJECTED}
    else:
        allowed = {EdgeCondition.SUCCESS, EdgeCondition.FAILURE}
    if edge.condition in allowed:
        return []
    return [
        ValidationIssue(
            code="invalid_compiled_condition",
            message=f"{source.node_type.value} cannot emit {edge.condition.value}",
            node_id=source.id,
            edge_id=edge.id,
        )
    ]


def _validate_fanout_and_joins(
    nodes: list[WorkflowNode],
    edges: list[WorkflowEdge],
) -> list[ValidationIssue]:
    errors: list[ValidationIssue] = []
    by_source_condition = Counter((edge.from_node, edge.condition) for edge in edges)
    for (node_id, condition), count in sorted(
        by_source_condition.items(), key=lambda item: (item[0][0], item[0][1].value)
    ):
        if count > 1:
            errors.append(
                _issue(
                    "outcome_fanout",
                    f"node has {count} {condition.value} edges; Demo permits one",
                    node_id,
                )
            )
    incoming = Counter(edge.to_node for edge in edges)
    for node in nodes:
        if incoming[node.id] > 1 and node.node_type != NodeType.OUTPUT:
            errors.append(
                _issue(
                    "unsupported_join",
                    "Demo permits multiple incoming edges only on output",
                    node.id,
                )
            )
    return errors


def _validate_outcome_branches(
    nodes: list[WorkflowNode],
    edges: list[WorkflowEdge],
) -> list[ValidationIssue]:
    errors: list[ValidationIssue] = []
    outgoing: dict[str, Counter[EdgeCondition]] = {}
    for edge in edges:
        outgoing.setdefault(edge.from_node, Counter())[edge.condition] += 1
    for node in nodes:
        counts = outgoing.get(node.id, Counter())
        if node.node_type == NodeType.IF:
            for condition in (EdgeCondition.MATCHED, EdgeCondition.NOT_MATCHED):
                if counts[condition] != 1:
                    errors.append(
                        _issue(
                            "if_branch_missing",
                            f"compiled if requires one {condition.value} edge",
                            node.id,
                        )
                    )
        if node.node_type == NodeType.APPROVAL:
            for condition in (EdgeCondition.APPROVED, EdgeCondition.REJECTED):
                if counts[condition] != 1:
                    errors.append(
                        _issue(
                            "approval_branch_missing",
                            f"approval requires one {condition.value} edge",
                            node.id,
                        )
                    )
    return errors


def _validate_security_chains(
    nodes: list[WorkflowNode],
    edges: list[WorkflowEdge],
) -> list[ValidationIssue]:
    errors: list[ValidationIssue] = []
    node_by_id = {node.id: node for node in nodes}
    outgoing: dict[str, list[WorkflowEdge]] = {}
    for edge in edges:
        outgoing.setdefault(edge.from_node, []).append(edge)
    output_ids = {node.id for node in nodes if node.node_type == NodeType.OUTPUT}

    for source in nodes:
        if source.node_type != NodeType.AGENT_TASK or not source.requires_write:
            continue
        system = [node for node in nodes if node.source_node_id == source.id]
        present = Counter(node.node_type for node in system)
        for required in (
            NodeType.PATCH_GUARD,
            NodeType.RISK_CLASSIFIER,
            NodeType.APPROVAL,
            NodeType.MERGE_PATCH,
        ):
            if present[required] != 1:
                errors.append(
                    _issue(
                        "security_chain_incomplete",
                        f"write task requires exactly one {required.value}",
                        source.id,
                    )
                )
        if present[NodeType.TEST] < 1:
            errors.append(
                _issue(
                    "security_chain_incomplete",
                    "write task requires at least one test",
                    source.id,
                )
            )
        rule_ids = [node.system_rule_id for node in system]
        if len(rule_ids) != len(set(rule_ids)):
            errors.append(
                _issue(
                    "duplicate_system_rule",
                    "write-task system_rule_id values must be unique",
                    source.id,
                )
            )
        if source.policy_risk_floor is not None and source.policy_risk_floor.value == "L4":
            errors.append(_issue("l4_risk_rejected", "L4 tasks are not executable", source.id))

        chain: list[WorkflowNode] = []
        current = source
        seen: set[str] = set()
        while True:
            happy_condition = (
                EdgeCondition.APPROVED
                if current.node_type == NodeType.APPROVAL
                else EdgeCondition.SUCCESS
            )
            happy_edges = [
                edge for edge in outgoing.get(current.id, []) if edge.condition == happy_condition
            ]
            if len(happy_edges) != 1:
                errors.append(
                    _issue(
                        "security_happy_path",
                        f"{current.node_type.value} requires exactly one "
                        f"{happy_condition.value} edge",
                        current.id,
                    )
                )
                break
            target = node_by_id[happy_edges[0].to_node]
            if target.node_type not in _SYSTEM_TYPES or target.source_node_id != source.id:
                break
            if target.id in seen:
                errors.append(
                    _issue("security_chain_cycle", "security chain repeats a node", target.id)
                )
                break
            seen.add(target.id)
            chain.append(target)
            current = target

        if not chain or chain[0].node_type != NodeType.PATCH_GUARD:
            errors.append(
                _issue(
                    "patch_guard_bypass",
                    "write task success must enter its sealed patch_guard",
                    source.id,
                )
            )
        if {node.id for node in chain} != {node.id for node in system}:
            errors.append(
                _issue(
                    "security_chain_disconnected",
                    "all same-source system nodes must be on one sealed happy path",
                    source.id,
                )
            )
        errors.extend(_validate_security_sequence(source, chain))

        for node in system:
            exit_condition = (
                EdgeCondition.REJECTED
                if node.node_type == NodeType.APPROVAL
                else EdgeCondition.FAILURE
            )
            exits = [edge for edge in outgoing.get(node.id, []) if edge.condition == exit_condition]
            if len(exits) != 1 or exits[0].to_node not in output_ids:
                errors.append(
                    _issue(
                        "security_terminal_exit",
                        f"{node.node_type.value} {exit_condition.value} must terminate at output",
                        node.id,
                    )
                )
    return errors


def _validate_security_sequence(
    source: WorkflowNode,
    chain: list[WorkflowNode],
) -> list[ValidationIssue]:
    if not chain:
        return []
    errors: list[ValidationIssue] = []
    types = [node.node_type for node in chain]
    if len(types) < 5 or types[-3:] != [
        NodeType.RISK_CLASSIFIER,
        NodeType.APPROVAL,
        NodeType.MERGE_PATCH,
    ]:
        errors.append(
            _issue(
                "security_chain_order",
                "security chain must end with risk_classifier -> approval -> merge_patch",
                source.id,
            )
        )
        return errors

    test_segment = chain[1:-3]
    if len(test_segment) == 1 and test_segment[0].node_type == NodeType.TEST:
        if test_segment[0].test_kind != TestKind.DOCS_STATIC:
            errors.append(
                _issue(
                    "security_chain_order",
                    "a test without command_guard must be docs_static",
                    test_segment[0].id,
                )
            )
        return errors

    if not test_segment or len(test_segment) % 2:
        errors.append(
            _issue(
                "security_chain_order",
                "command security chain must contain command_guard/test pairs",
                source.id,
            )
        )
        return errors
    for index in range(0, len(test_segment), 2):
        guard, test = test_segment[index : index + 2]
        if (
            guard.node_type != NodeType.COMMAND_GUARD
            or test.node_type != NodeType.TEST
            or test.test_kind != TestKind.COMMAND
        ):
            errors.append(
                _issue(
                    "security_chain_order",
                    "command_guard must immediately precede its command test",
                    test.id,
                )
            )
    return errors


def _pure_docs_scope(node: WorkflowNode) -> bool:
    paths = list(node.effective_allowed_files or []) + list(node.effective_new_files or [])
    if not paths:
        return False
    for value in paths:
        path = PurePosixPath(value)
        stem = path.stem.casefold()
        if path.suffix.casefold() not in _DOC_EXTENSIONS:
            return False
        if path.name.casefold() in _SENSITIVE_DOC_NAMES or any(
            stem == token or stem.startswith(f"{token}-") or stem.startswith(f"{token}_")
            for token in _SENSITIVE_DOC_STEMS
        ):
            return False
    return True


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


def _reachable(start: str, edges: list[WorkflowEdge]) -> set[str]:
    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        outgoing.setdefault(edge.from_node, []).append(edge.to_node)
    result = {start}
    queue = deque([start])
    while queue:
        for target in outgoing.get(queue.popleft(), []):
            if target not in result:
                result.add(target)
                queue.append(target)
    return result


def _reverse_reachable(target: str, edges: list[WorkflowEdge]) -> set[str]:
    incoming: dict[str, list[str]] = {}
    for edge in edges:
        incoming.setdefault(edge.to_node, []).append(edge.from_node)
    result = {target}
    queue = deque([target])
    while queue:
        for source in incoming.get(queue.popleft(), []):
            if source not in result:
                result.add(source)
                queue.append(source)
    return result


def _issue(code: str, message: str, node_id: str | None = None) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, node_id=node_id)


__all__ = ["MAX_COMPILED_EDGES", "MAX_COMPILED_NODES", "ExecutableValidator"]
