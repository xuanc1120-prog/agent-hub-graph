from __future__ import annotations

from master.router import AgentCapability, AgentCatalog
from protocol import (
    AuthorGraph,
    CompiledGraph,
    EdgeCondition,
    NodeType,
    RiskLevel,
    TaskKind,
    WorkflowEdge,
)
from protocol import (
    TestKind as WorkflowTestKind,
)
from workflow.compiler import WorkflowCompiler
from workflow.executable_validator import ExecutableValidator
from workflow.registry import NODE_CONFIG_MODELS, NodeRegistry

BASE_COMMIT = "a" * 40


class _DummyHandler:
    async def execute(self, context: object) -> object:
        return context


def _registry() -> NodeRegistry:
    registry = NodeRegistry()
    for node_type, model in NODE_CONFIG_MODELS.items():
        registry.register(node_type, model, _DummyHandler())
    return registry


def _compile_write(
    author: AuthorGraph,
    catalog: AgentCatalog,
    *,
    commands: tuple[tuple[str, ...], ...],
    task_kind: TaskKind = TaskKind.IMPLEMENT,
    risk: RiskLevel = RiskLevel.L3,
    new_file: str = "src/new.py",
) -> CompiledGraph:
    graph = author.model_copy(deep=True)
    task = next(node for node in graph.nodes if node.node_type == NodeType.AGENT_TASK)
    task.task_kind = task_kind
    task.requires_write = True
    task.risk_level_hint = risk
    task.allowed_files_candidate = ["docs/guide.md"] if task_kind == TaskKind.DOCS else []
    task.new_files_candidate = [] if task_kind == TaskKind.DOCS else [new_file]
    task.allowed_commands_candidate = [list(command) for command in commands]
    agent = catalog.agents[0].model_copy(
        update={
            "capabilities": catalog.agents[0].capabilities.union(
                {AgentCapability.WRITE_FILES, AgentCapability.GENERATE_PATCH}
            )
        }
    )
    write_catalog = catalog.model_copy(update={"agents": (agent,)})
    result = WorkflowCompiler(write_catalog).compile(
        graph,
        integration_base_commit=BASE_COMMIT,
    )
    assert result.graph is not None
    assert not result.errors
    assert ExecutableValidator(_registry(), write_runtime_enabled=True).validate(result.graph).ok
    return result.graph


def _validation_codes(graph: CompiledGraph) -> set[str]:
    return {
        issue.code
        for issue in ExecutableValidator(
            _registry(),
            write_runtime_enabled=True,
        )
        .validate(graph)
        .errors
    }


def test_rejects_unapproved_and_duplicate_test_argv(
    readonly_graph: AuthorGraph,
    mock_catalog: AgentCatalog,
) -> None:
    compiled = _compile_write(
        readonly_graph,
        mock_catalog,
        commands=(("pytest", "-q"), ("ruff", "check", ".")),
    )
    tests = sorted(
        (node for node in compiled.nodes if node.test_kind == WorkflowTestKind.COMMAND),
        key=lambda node: node.system_rule_id or "",
    )

    unapproved = compiled.model_copy(deep=True)
    first = next(node for node in unapproved.nodes if node.id == tests[0].id)
    first.test_argv = ["python", "-c", "print('unapproved')"]
    assert "security_test_commands_mismatch" in _validation_codes(unapproved)

    duplicate = compiled.model_copy(deep=True)
    first = next(node for node in duplicate.nodes if node.id == tests[0].id)
    second = next(node for node in duplicate.nodes if node.id == tests[1].id)
    second.test_argv = list(first.test_argv or [])
    assert "security_test_commands_mismatch" in _validation_codes(duplicate)


def test_rejects_missing_command_test(
    readonly_graph: AuthorGraph,
    mock_catalog: AgentCatalog,
) -> None:
    compiled = _compile_write(
        readonly_graph,
        mock_catalog,
        commands=(("pytest", "-q"), ("ruff", "check", ".")),
    )
    tampered = compiled.model_copy(deep=True)
    tests = sorted(
        (node for node in tampered.nodes if node.test_kind == WorkflowTestKind.COMMAND),
        key=lambda node: node.system_rule_id or "",
    )
    removed_test = tests[1]
    removed_guard = next(
        node for node in tampered.nodes if node.system_rule_id == "write.command_guard.2.v1"
    )
    removed_ids = {removed_guard.id, removed_test.id}
    risk = next(node for node in tampered.nodes if node.node_type == NodeType.RISK_CLASSIFIER)
    tampered.nodes = [node for node in tampered.nodes if node.id not in removed_ids]
    tampered.edges = [
        edge
        for edge in tampered.edges
        if edge.from_node not in removed_ids and edge.to_node not in removed_ids
    ]
    tampered.edges.append(
        WorkflowEdge(
            id="tampered-test-resume",
            from_node=tests[0].id,
            to_node=risk.id,
            condition=EdgeCondition.SUCCESS,
            system_managed=True,
        )
    )

    assert "security_test_commands_mismatch" in _validation_codes(tampered)


def test_rejects_synchronously_lowered_risk_floor_and_rule_id(
    readonly_graph: AuthorGraph,
    mock_catalog: AgentCatalog,
) -> None:
    compiled = _compile_write(
        readonly_graph,
        mock_catalog,
        commands=(("pytest", "-q"),),
    )
    tampered = compiled.model_copy(deep=True)
    source = next(node for node in tampered.nodes if node.node_type == NodeType.AGENT_TASK)
    source.policy_risk_floor = RiskLevel.L0
    for node in tampered.nodes:
        if node.source_node_id == source.id:
            node.policy_risk_floor = RiskLevel.L0
    patch = next(node for node in tampered.nodes if node.node_type == NodeType.PATCH_GUARD)
    patch.system_rule_id = "write.patch_guard.downgraded.v1"

    codes = _validation_codes(tampered)

    assert "compiled_risk_floor_mismatch" in codes
    assert "security_rule_mismatch" in codes


def test_docs_with_approved_commands_uses_command_tests(
    readonly_graph: AuthorGraph,
    mock_catalog: AgentCatalog,
) -> None:
    compiled = _compile_write(
        readonly_graph,
        mock_catalog,
        task_kind=TaskKind.DOCS,
        commands=(("pytest", "tests/docs"),),
    )

    tests = [node for node in compiled.nodes if node.node_type == NodeType.TEST]

    assert [node.test_kind for node in tests] == [WorkflowTestKind.COMMAND]


def test_static_path_risk_roundtrips_executable_validation(
    readonly_graph: AuthorGraph,
    mock_catalog: AgentCatalog,
) -> None:
    compiled = _compile_write(
        readonly_graph,
        mock_catalog,
        commands=(("pytest", "-q", "tests/test_new.py"),),
        task_kind=TaskKind.TEST_FIX,
        risk=RiskLevel.L1,
        new_file="tests/test_new.py",
    )
    source = next(node for node in compiled.nodes if node.node_type == NodeType.AGENT_TASK)
    assert source.policy_risk_floor == RiskLevel.L2

    tampered = compiled.model_copy(deep=True)
    tampered_source = next(node for node in tampered.nodes if node.id == source.id)
    tampered_source.policy_risk_floor = RiskLevel.L1
    for node in tampered.nodes:
        if node.source_node_id == source.id:
            node.policy_risk_floor = RiskLevel.L1

    assert "compiled_risk_floor_mismatch" in _validation_codes(tampered)
