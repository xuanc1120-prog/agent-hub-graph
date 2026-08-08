"""Deterministic AuthorGraph to CompiledGraph compilation."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from itertools import pairwise
from pathlib import PurePosixPath

from master.router import AgentCatalog, AgentRouter, RoutingDecision
from protocol import (
    AgentRecommendation,
    AuthorGraph,
    CompiledGraph,
    EdgeCondition,
    GitObjectId,
    NodeType,
    RiskLevel,
    Sha256Hex,
    TaskKind,
    TestKind,
    ValidationIssue,
    WorkflowEdge,
    WorkflowNode,
    canonical_json,
)
from security.command_guard import CommandGuard, CommandGuardViolation
from security.path_policy import PathPolicy, PathPolicyViolation
from security.risk_classifier import RiskClassifier
from workflow.graph_model import normalize_author_graph, normalize_command_templates
from workflow.validation import DraftValidator

_RISK_ORDER = {
    RiskLevel.L0: 0,
    RiskLevel.L1: 1,
    RiskLevel.L2: 2,
    RiskLevel.L3: 3,
    RiskLevel.L4: 4,
}
_DOC_EXTENSIONS = frozenset({".md", ".mdx", ".rst", ".txt"})
_SENSITIVE_DOC_STEMS = frozenset({"requirements", "constraints", "manifest", "config", "policy"})
_SENSITIVE_DOC_NAMES = frozenset({"package.json", "pyproject.toml"})


@dataclass(frozen=True, slots=True)
class CompilationResult:
    graph: CompiledGraph | None
    compiled_hash: str | None
    source_author_hash: str
    errors: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return self.graph is not None and not self.errors


class PolicyInjector:
    """Inject sealed write-task security chains with deterministic IDs."""

    def inject(
        self,
        nodes: list[WorkflowNode],
        edges: list[WorkflowEdge],
    ) -> tuple[list[WorkflowNode], list[WorkflowEdge], list[ValidationIssue]]:
        output_nodes = [node for node in nodes if node.node_type == NodeType.OUTPUT]
        if len(output_nodes) != 1:
            return nodes, edges, []
        output_id = output_nodes[0].id
        errors: list[ValidationIssue] = []
        result_nodes = list(nodes)
        result_edges = list(edges)

        write_nodes = sorted(
            (
                node
                for node in nodes
                if node.node_type == NodeType.AGENT_TASK and node.requires_write
            ),
            key=lambda item: item.id,
        )
        for source in write_nodes:
            original_success = sorted(
                (
                    edge
                    for edge in result_edges
                    if edge.from_node == source.id and edge.condition == EdgeCondition.SUCCESS
                ),
                key=lambda item: item.id,
            )
            result_edges = [edge for edge in result_edges if edge not in original_success]
            chain_nodes, chain_edges, chain_errors = self._build_chain(
                source,
                original_success,
                output_id,
            )
            result_nodes.extend(chain_nodes)
            result_edges.extend(chain_edges)
            errors.extend(chain_errors)

        return result_nodes, result_edges, errors

    def _build_chain(
        self,
        source: WorkflowNode,
        original_success: list[WorkflowEdge],
        output_id: str,
    ) -> tuple[list[WorkflowNode], list[WorkflowEdge], list[ValidationIssue]]:
        assert source.task_kind is not None
        nodes: list[WorkflowNode] = []
        edges: list[WorkflowEdge] = []
        errors: list[ValidationIssue] = []
        ordinal = 0

        def add_node(
            node_type: NodeType,
            rule: str,
            title: str,
            *,
            test_kind: TestKind | None = None,
            test_argv: list[str] | None = None,
        ) -> WorkflowNode:
            nonlocal ordinal
            ordinal += 1
            node = WorkflowNode(
                id=_system_id(source.id, rule, ordinal),
                node_type=node_type,
                title=title,
                system_managed=True,
                source_node_id=source.id,
                system_rule_id=rule,
                policy_risk_floor=source.policy_risk_floor,
                test_kind=test_kind,
                test_argv=test_argv,
            )
            nodes.append(node)
            return node

        patch = add_node(NodeType.PATCH_GUARD, "write.patch_guard.v1", "Validate task patch")
        sequence = [patch]

        commands = list(source.effective_allowed_commands or [])
        if _is_docs_static(source):
            sequence.append(
                add_node(
                    NodeType.TEST,
                    "write.docs_static_test.v1",
                    "Validate documentation files",
                    test_kind=TestKind.DOCS_STATIC,
                )
            )
        elif commands:
            for index, argv in enumerate(commands, start=1):
                sequence.append(
                    add_node(
                        NodeType.COMMAND_GUARD,
                        f"write.command_guard.{index}.v1",
                        f"Validate test command {index}",
                    )
                )
                sequence.append(
                    add_node(
                        NodeType.TEST,
                        f"write.command_test.{index}.v1",
                        f"Run approved test {index}",
                        test_kind=TestKind.COMMAND,
                        test_argv=list(argv),
                    )
                )
        else:
            sequence.append(
                add_node(
                    NodeType.COMMAND_GUARD,
                    "write.command_guard.missing.v1",
                    "Require an approved test command",
                )
            )
            sequence.append(
                add_node(
                    NodeType.TEST,
                    "write.command_test.missing.v1",
                    "Blocked: approved test command missing",
                    test_kind=TestKind.COMMAND,
                )
            )
            errors.append(
                _issue(
                    "test_command_missing",
                    "write task has no approved test command candidate",
                    source.id,
                )
            )

        risk = add_node(
            NodeType.RISK_CLASSIFIER,
            "write.risk_classifier.v1",
            "Classify final change risk",
        )
        approval = add_node(
            NodeType.APPROVAL,
            "write.changeset_approval.v1",
            "Approve final change set",
        )
        merge = add_node(NodeType.MERGE_PATCH, "write.merge_patch.v1", "Merge approved patch")
        sequence.extend((risk, approval, merge))

        edges.append(_system_edge(source.id, patch.id, source.id, "enter", 0))
        for index, (left, right) in enumerate(pairwise(sequence), start=1):
            condition = (
                EdgeCondition.APPROVED
                if left.node_type == NodeType.APPROVAL
                else EdgeCondition.SUCCESS
            )
            edges.append(
                _system_edge(left.id, right.id, source.id, "chain", index, condition=condition)
            )

        edges.append(
            _system_edge(
                approval.id,
                output_id,
                source.id,
                "approval-rejected",
                0,
                condition=EdgeCondition.REJECTED,
            )
        )
        for index, node in enumerate(sequence, start=1):
            if node.node_type == NodeType.APPROVAL:
                continue
            edges.append(
                _system_edge(
                    node.id,
                    output_id,
                    source.id,
                    "failure-output",
                    index,
                    condition=EdgeCondition.FAILURE,
                )
            )

        if original_success:
            for index, original in enumerate(original_success, start=1):
                edges.append(
                    _system_edge(
                        merge.id,
                        original.to_node,
                        source.id,
                        "resume-success",
                        index,
                    )
                )
        else:
            edges.append(_system_edge(merge.id, output_id, source.id, "finish", 0))
        return nodes, edges, errors


class WorkflowCompiler:
    """Compile an AuthorGraph using an immutable policy and agent catalog."""

    def __init__(
        self,
        catalog: AgentCatalog,
        *,
        policy_version: str = "demo-v1",
        validator: DraftValidator | None = None,
        injector: PolicyInjector | None = None,
        path_policy: PathPolicy | None = None,
        command_guard: CommandGuard | None = None,
        risk_classifier: RiskClassifier | None = None,
    ) -> None:
        if not policy_version or len(policy_version) > 64:
            raise ValueError("policy_version must contain 1..64 characters")
        self._catalog = catalog
        self._policy_version = policy_version
        self._validator = validator or DraftValidator()
        self._injector = injector or PolicyInjector()
        self._router = AgentRouter(catalog)
        self._path_policy = path_policy
        self._command_guard = command_guard
        self._risk_classifier = risk_classifier or RiskClassifier()

    def compile(
        self,
        author_graph: AuthorGraph,
        *,
        integration_base_commit: GitObjectId,
    ) -> CompilationResult:
        draft_report = self._validator.validate(author_graph, require_complete=True)
        normalized_author = normalize_author_graph(author_graph)
        source_hash = sha256(canonical_json(normalized_author)).hexdigest()
        if not draft_report.ok:
            return CompilationResult(
                graph=None,
                compiled_hash=None,
                source_author_hash=source_hash,
                errors=draft_report.errors,
                warnings=draft_report.warnings,
            )

        compiled_nodes: list[WorkflowNode] = []
        errors: list[ValidationIssue] = []
        warnings = list(draft_report.warnings)
        for node in normalized_author.nodes:
            if node.node_type != NodeType.AGENT_TASK:
                compiled_nodes.append(node.model_copy(deep=True))
                continue
            routed = self._router.route(node)
            if routed.decision != RoutingDecision.ASSIGNED:
                errors.append(
                    _issue(
                        "agent_unavailable",
                        routed.blocked_reason or "agent routing failed",
                        node.id,
                    )
                )
                continue
            try:
                if self._path_policy is None:
                    existing = sorted(set(node.allowed_files_candidate))
                    new = sorted(set(node.new_files_candidate))
                else:
                    scope = self._path_policy.validate_scope(
                        node.allowed_files_candidate,
                        node.new_files_candidate,
                    )
                    existing = list(scope.existing_files)
                    new = list(scope.new_files)
            except PathPolicyViolation as error:
                errors.append(_issue("path_policy_rejected", str(error), node.id))
                continue
            if len(existing) + len(new) > 100:
                errors.append(
                    _issue(
                        "effective_file_scope_limit",
                        "compiled file scope cannot exceed 100 exact paths",
                        node.id,
                    )
                )
                continue

            commands = normalize_command_templates(node.allowed_commands_candidate)
            if self._command_guard is not None:
                try:
                    approved_commands = self._command_guard.validate_many(commands)
                except CommandGuardViolation as error:
                    errors.append(_issue("command_guard_rejected", str(error), node.id))
                    continue
                commands = [list(command.argv) for command in approved_commands]
            elif len(commands) > 20:
                errors.append(
                    _issue(
                        "effective_command_scope_limit",
                        "compiled command scope cannot exceed 20 argv templates",
                        node.id,
                    )
                )
                continue
            if node.requires_write and not existing and not new:
                errors.append(
                    _issue(
                        "write_scope_empty",
                        "write task requires at least one exact existing or new file",
                        node.id,
                    )
                )
            candidate_risk = (
                self._risk_classifier.classify_paths([*existing, *new]).effective_risk
                if node.requires_write
                else RiskLevel.L0
            )
            risk_floor = _max_risk(
                _max_risk(node.risk_level_hint, candidate_risk),
                RiskLevel.L1 if node.requires_write else RiskLevel.L0,
            )
            if risk_floor == RiskLevel.L4:
                errors.append(
                    _issue("l4_risk_rejected", "L4 tasks are rejected by policy", node.id)
                )
            compiled_nodes.append(
                node.model_copy(
                    update={
                        "resolved_agent_id": routed.resolved_agent_id,
                        "resolved_agent_spec_sha256": routed.resolved_agent_spec_sha256,
                        "recommended_agents": sorted(
                            (
                                AgentRecommendation(
                                    agent_id=item.agent_id,
                                    score=item.score,
                                    reason=item.reason,
                                )
                                for item in routed.ranked_candidates
                            ),
                            key=lambda item: item.agent_id,
                        ),
                        "effective_allowed_files": existing,
                        "effective_new_files": new,
                        "effective_allowed_commands": commands,
                        "policy_risk_floor": risk_floor,
                        "requires_changeset_approval": node.requires_write,
                    },
                    deep=True,
                )
            )

        if errors:
            return CompilationResult(
                graph=None,
                compiled_hash=None,
                source_author_hash=source_hash,
                errors=tuple(errors),
                warnings=tuple(warnings),
            )

        injected_nodes, injected_edges, injection_errors = self._injector.inject(
            compiled_nodes,
            list(normalized_author.edges),
        )
        errors.extend(injection_errors)
        graph = CompiledGraph(
            source_author_hash=Sha256Hex(source_hash),
            integration_base_commit=integration_base_commit,
            policy_version=self._policy_version,
            agent_catalog_snapshot_hash=self._catalog.catalog_hash,
            nodes=sorted(injected_nodes, key=lambda item: item.id),
            edges=sorted(injected_edges, key=lambda item: item.id),
        )
        compiled_hash = sha256(canonical_json(graph)).hexdigest()
        return CompilationResult(
            graph=graph,
            compiled_hash=compiled_hash,
            source_author_hash=source_hash,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )


def _max_risk(left: RiskLevel, right: RiskLevel) -> RiskLevel:
    return left if _RISK_ORDER[left] >= _RISK_ORDER[right] else right


def _is_docs_static(node: WorkflowNode) -> bool:
    if node.task_kind != TaskKind.DOCS:
        return False
    if node.effective_allowed_commands:
        return False
    paths = list(node.effective_allowed_files or []) + list(node.effective_new_files or [])
    if not paths:
        return False
    for value in paths:
        path = PurePosixPath(value)
        if path.suffix.casefold() not in _DOC_EXTENSIONS:
            return False
        stem = path.stem.casefold()
        if path.name.casefold() in _SENSITIVE_DOC_NAMES or any(
            stem == token or stem.startswith(f"{token}-") or stem.startswith(f"{token}_")
            for token in _SENSITIVE_DOC_STEMS
        ):
            return False
    return True


def _system_id(source_node_id: str, rule: str, ordinal: int) -> str:
    digest = sha256(f"{source_node_id}\x00{rule}\x00{ordinal}".encode()).hexdigest()[:20]
    return f"sys-{digest}"


def _system_edge(
    from_node: str,
    to_node: str,
    source_node_id: str,
    rule: str,
    ordinal: int,
    *,
    condition: EdgeCondition = EdgeCondition.SUCCESS,
) -> WorkflowEdge:
    digest = sha256(
        f"{source_node_id}\x00{rule}\x00{ordinal}\x00{from_node}\x00{to_node}\x00{condition.value}".encode()
    ).hexdigest()[:20]
    return WorkflowEdge(
        id=f"sys-edge-{digest}",
        from_node=from_node,
        to_node=to_node,
        condition=condition,
        system_managed=True,
    )


def _issue(code: str, message: str, node_id: str | None = None) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, node_id=node_id)


__all__ = [
    "CompilationResult",
    "PolicyInjector",
    "WorkflowCompiler",
]
