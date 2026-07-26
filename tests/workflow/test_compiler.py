from __future__ import annotations

from master.router import AgentCapability, AgentCatalog
from protocol import AuthorGraph, NodeType, RiskLevel, TaskKind
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


def test_readonly_compile_is_deterministic_and_executable(
    readonly_graph: AuthorGraph,
    mock_catalog: AgentCatalog,
) -> None:
    reordered = AuthorGraph(
        nodes=list(reversed(readonly_graph.nodes)),
        edges=list(reversed(readonly_graph.edges)),
    )
    compiler = WorkflowCompiler(mock_catalog)

    first = compiler.compile(readonly_graph, integration_base_commit=BASE_COMMIT)
    second = compiler.compile(reordered, integration_base_commit=BASE_COMMIT)

    assert first.ok
    assert second.ok
    assert first.compiled_hash == second.compiled_hash
    assert first.source_author_hash == second.source_author_hash
    assert first.graph == second.graph
    assert ExecutableValidator(_registry()).validate(first.graph).ok
    task = next(node for node in first.graph.nodes if node.node_type == NodeType.AGENT_TASK)
    assert task.resolved_agent_id == "mock"
    assert task.policy_risk_floor == RiskLevel.L0
    assert task.requires_changeset_approval is False


def test_compile_does_not_normalize_away_duplicate_scope(
    readonly_graph: AuthorGraph,
    mock_catalog: AgentCatalog,
) -> None:
    graph = readonly_graph.model_copy(deep=True)
    graph.nodes[1].allowed_files_candidate = ["src/example.py", "src/example.py"]

    result = WorkflowCompiler(mock_catalog).compile(
        graph,
        integration_base_commit=BASE_COMMIT,
    )

    assert result.graph is None
    assert "duplicate_candidate_path" in {issue.code for issue in result.errors}


def test_write_compile_injects_sealed_chain_but_runtime_fails_closed(
    readonly_graph: AuthorGraph,
    mock_catalog: AgentCatalog,
) -> None:
    graph = readonly_graph.model_copy(deep=True)
    task = graph.nodes[1]
    task.task_kind = TaskKind.IMPLEMENT
    task.requires_write = True
    task.risk_level_hint = RiskLevel.L2
    task.new_files_candidate = ["src/new.py"]
    task.allowed_commands_candidate = [["pytest", "-q"]]
    write_catalog = mock_catalog.model_copy(deep=True)
    write_agent = write_catalog.agents[0].model_copy(
        update={
            "capabilities": write_catalog.agents[0].capabilities.union(
                {AgentCapability.WRITE_FILES, AgentCapability.GENERATE_PATCH}
            )
        }
    )
    write_catalog = write_catalog.model_copy(update={"agents": (write_agent,)})

    result = WorkflowCompiler(write_catalog).compile(
        graph,
        integration_base_commit=BASE_COMMIT,
    )

    assert result.graph is not None
    assert not result.errors
    source_ids = {node.node_type for node in result.graph.nodes if node.source_node_id == "analyze"}
    assert {
        NodeType.PATCH_GUARD,
        NodeType.COMMAND_GUARD,
        NodeType.TEST,
        NodeType.RISK_CLASSIFIER,
        NodeType.APPROVAL,
        NodeType.MERGE_PATCH,
    }.issubset(source_ids)
    report = ExecutableValidator(_registry(), write_runtime_enabled=False).validate(result.graph)
    assert "write_runtime_unavailable" in {issue.code for issue in report.errors}


def test_write_without_test_command_is_blocked_at_compile(
    readonly_graph: AuthorGraph,
    mock_catalog: AgentCatalog,
) -> None:
    graph = readonly_graph.model_copy(deep=True)
    task = graph.nodes[1]
    task.requires_write = True
    task.new_files_candidate = ["src/new.py"]
    write_agent = mock_catalog.agents[0].model_copy(
        update={
            "capabilities": mock_catalog.agents[0].capabilities.union(
                {AgentCapability.WRITE_FILES, AgentCapability.GENERATE_PATCH}
            )
        }
    )
    catalog = mock_catalog.model_copy(update={"agents": (write_agent,)})

    result = WorkflowCompiler(catalog).compile(graph, integration_base_commit=BASE_COMMIT)

    assert result.graph is not None
    assert "test_command_missing" in {issue.code for issue in result.errors}


def test_compile_reports_effective_scope_limits_without_protocol_exception(
    readonly_graph: AuthorGraph,
    mock_catalog: AgentCatalog,
) -> None:
    graph = readonly_graph.model_copy(deep=True)
    graph.nodes[1].allowed_files_candidate = [f"src/file-{index}.py" for index in range(101)]

    result = WorkflowCompiler(mock_catalog).compile(
        graph,
        integration_base_commit=BASE_COMMIT,
    )

    assert result.graph is None
    assert {issue.code for issue in result.errors} == {"effective_file_scope_limit"}


def test_compile_reports_command_scope_limit_without_protocol_exception(
    readonly_graph: AuthorGraph,
    mock_catalog: AgentCatalog,
) -> None:
    graph = readonly_graph.model_copy(deep=True)
    task = graph.nodes[1]
    task.task_kind = TaskKind.IMPLEMENT
    task.requires_write = True
    task.new_files_candidate = ["src/new.py"]
    task.allowed_commands_candidate = [["pytest", f"tests/test_{index}.py"] for index in range(21)]
    write_agent = mock_catalog.agents[0].model_copy(
        update={
            "capabilities": mock_catalog.agents[0].capabilities.union(
                {AgentCapability.WRITE_FILES, AgentCapability.GENERATE_PATCH}
            )
        }
    )
    catalog = mock_catalog.model_copy(update={"agents": (write_agent,)})

    result = WorkflowCompiler(catalog).compile(graph, integration_base_commit=BASE_COMMIT)

    assert result.graph is None
    assert {issue.code for issue in result.errors} == {"effective_command_scope_limit"}


def test_executable_validator_rejects_compiler_scope_widening(
    readonly_graph: AuthorGraph,
    mock_catalog: AgentCatalog,
) -> None:
    compiled = WorkflowCompiler(mock_catalog).compile(
        readonly_graph,
        integration_base_commit=BASE_COMMIT,
    )
    assert compiled.graph is not None
    tampered = compiled.graph.model_copy(deep=True)
    task = next(node for node in tampered.nodes if node.node_type == NodeType.AGENT_TASK)
    task.effective_allowed_files = ["secrets.txt"]

    codes = {
        issue.code
        for issue in ExecutableValidator(
            _registry(),
            write_runtime_enabled=True,
        )
        .validate(tampered)
        .errors
    }

    assert "compiled_scope_widened" in codes


def test_executable_validator_rejects_disconnected_write_security_chain(
    readonly_graph: AuthorGraph,
    mock_catalog: AgentCatalog,
) -> None:
    graph = readonly_graph.model_copy(deep=True)
    task = graph.nodes[1]
    task.task_kind = TaskKind.IMPLEMENT
    task.requires_write = True
    task.new_files_candidate = ["src/new.py"]
    task.allowed_commands_candidate = [["pytest", "-q"]]
    write_agent = mock_catalog.agents[0].model_copy(
        update={
            "capabilities": mock_catalog.agents[0].capabilities.union(
                {AgentCapability.WRITE_FILES, AgentCapability.GENERATE_PATCH}
            )
        }
    )
    catalog = mock_catalog.model_copy(update={"agents": (write_agent,)})
    compiled = WorkflowCompiler(catalog).compile(graph, integration_base_commit=BASE_COMMIT)
    assert compiled.graph is not None
    tampered = compiled.graph.model_copy(deep=True)
    patch = next(node for node in tampered.nodes if node.node_type == NodeType.PATCH_GUARD)
    risk = next(node for node in tampered.nodes if node.node_type == NodeType.RISK_CLASSIFIER)
    happy_edge = next(
        edge
        for edge in tampered.edges
        if edge.from_node == patch.id and edge.condition.value == "success"
    )
    happy_edge.to_node = risk.id

    codes = {
        issue.code
        for issue in ExecutableValidator(_registry(), write_runtime_enabled=True)
        .validate(tampered)
        .errors
    }

    assert "security_chain_disconnected" in codes
    assert "security_chain_order" in codes


def test_node_registry_rejects_synchronous_handlers() -> None:
    class SyncHandler:
        def execute(self, context: object) -> object:
            return context

    registry = NodeRegistry()

    try:
        registry.register(NodeType.INPUT, NODE_CONFIG_MODELS[NodeType.INPUT], SyncHandler())
    except TypeError as error:
        assert "async execute" in str(error)
    else:
        raise AssertionError("synchronous handler registration was accepted")
