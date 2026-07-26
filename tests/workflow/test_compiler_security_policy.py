from __future__ import annotations

from pathlib import Path

from master.router import AgentCapability, AgentCatalog
from protocol import AuthorGraph, RiskLevel, TaskKind
from security.command_guard import CommandGuard
from security.path_policy import PathPolicy
from workflow.compiler import WorkflowCompiler

_BASE_COMMIT = "a" * 40


def _write_catalog(catalog: AgentCatalog) -> AgentCatalog:
    agent = catalog.agents[0].model_copy(
        update={
            "capabilities": catalog.agents[0].capabilities.union(
                {AgentCapability.WRITE_FILES, AgentCapability.GENERATE_PATCH}
            )
        }
    )
    return catalog.model_copy(update={"agents": (agent,)})


def _write_graph(graph: AuthorGraph) -> AuthorGraph:
    result = graph.model_copy(deep=True)
    task = result.nodes[1]
    task.task_kind = TaskKind.IMPLEMENT
    task.requires_write = True
    task.allowed_files_candidate = ["src/example.py"]
    task.new_files_candidate = ["src/new.py"]
    task.allowed_commands_candidate = [["pytest", "-q"]]
    return result


def _compiler(catalog: AgentCatalog, repo: Path) -> WorkflowCompiler:
    return WorkflowCompiler(
        _write_catalog(catalog),
        path_policy=PathPolicy(repo),
        command_guard=CommandGuard(),
    )


def test_compiler_freezes_only_semantically_valid_exact_scope(
    fixture_source_repo: Path,
    readonly_graph: AuthorGraph,
    mock_catalog: AgentCatalog,
) -> None:
    result = _compiler(mock_catalog, fixture_source_repo).compile(
        _write_graph(readonly_graph),
        integration_base_commit=_BASE_COMMIT,
    )

    assert result.ok
    assert result.graph is not None
    task = next(node for node in result.graph.nodes if node.id == "analyze")
    assert task.effective_allowed_files == ["src/example.py"]
    assert task.effective_new_files == ["src/new.py"]
    assert task.effective_allowed_commands == [["pytest", "-q"]]


def test_compiler_rejects_missing_existing_and_existing_new_path(
    fixture_source_repo: Path,
    readonly_graph: AuthorGraph,
    mock_catalog: AgentCatalog,
) -> None:
    missing = _write_graph(readonly_graph)
    missing.nodes[1].allowed_files_candidate = ["src/missing.py"]
    colliding = _write_graph(readonly_graph)
    colliding.nodes[1].allowed_files_candidate = []
    colliding.nodes[1].new_files_candidate = ["src/example.py"]
    compiler = _compiler(mock_catalog, fixture_source_repo)

    missing_result = compiler.compile(missing, integration_base_commit=_BASE_COMMIT)
    colliding_result = compiler.compile(colliding, integration_base_commit=_BASE_COMMIT)

    assert "path_policy_rejected" in {issue.code for issue in missing_result.errors}
    assert "path_policy_rejected" in {issue.code for issue in colliding_result.errors}


def test_compiler_rejects_sensitive_scope_and_unsafe_command(
    fixture_source_repo: Path,
    readonly_graph: AuthorGraph,
    mock_catalog: AgentCatalog,
) -> None:
    sensitive = _write_graph(readonly_graph)
    sensitive.nodes[1].new_files_candidate = [".env"]
    unsafe_command = _write_graph(readonly_graph)
    unsafe_command.nodes[1].allowed_commands_candidate = [["pytest", "-s"]]
    compiler = _compiler(mock_catalog, fixture_source_repo)

    sensitive_result = compiler.compile(sensitive, integration_base_commit=_BASE_COMMIT)
    command_result = compiler.compile(unsafe_command, integration_base_commit=_BASE_COMMIT)

    assert "path_policy_rejected" in {issue.code for issue in sensitive_result.errors}
    assert "command_guard_rejected" in {issue.code for issue in command_result.errors}


def test_compiler_static_policy_raises_test_paths_to_l2(
    fixture_source_repo: Path,
    readonly_graph: AuthorGraph,
    mock_catalog: AgentCatalog,
) -> None:
    graph = _write_graph(readonly_graph)
    graph.nodes[1].allowed_files_candidate = []
    graph.nodes[1].new_files_candidate = ["tests/test_new.py"]

    result = _compiler(mock_catalog, fixture_source_repo).compile(
        graph,
        integration_base_commit=_BASE_COMMIT,
    )

    assert result.ok
    assert result.graph is not None
    task = next(node for node in result.graph.nodes if node.id == "analyze")
    assert task.policy_risk_floor == RiskLevel.L2
