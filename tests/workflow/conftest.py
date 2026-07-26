from __future__ import annotations

import pytest
import pytest_asyncio

from master.router import AgentCapability, AgentCatalog, AgentSpec
from protocol import (
    AuthorGraph,
    EdgeCondition,
    NodeType,
    RiskLevel,
    TaskKind,
    WorkflowEdge,
    WorkflowNode,
)
from storage.db import Database


@pytest.fixture
def mock_catalog() -> AgentCatalog:
    return AgentCatalog(
        agents=(
            AgentSpec(
                agent_id="mock",
                display_name="Mock Agent",
                adapter_type="mock",
                spec_sha256="1" * 64,
                capabilities=frozenset(
                    {
                        AgentCapability.READ_CODE,
                        AgentCapability.ANALYZE,
                        AgentCapability.IMPLEMENT,
                        AgentCapability.REVIEW,
                        AgentCapability.DOCS,
                        AgentCapability.RUN_TESTS,
                    }
                ),
            ),
        ),
        catalog_hash="2" * 64,
    )


@pytest.fixture
def readonly_graph() -> AuthorGraph:
    return AuthorGraph(
        nodes=[
            WorkflowNode(id="input", node_type=NodeType.INPUT, title="Input"),
            WorkflowNode(
                id="analyze",
                node_type=NodeType.AGENT_TASK,
                task_kind=TaskKind.ANALYZE,
                title="Analyze",
                instruction="Analyze the fixture without modifying files.",
                risk_level_hint=RiskLevel.L0,
            ),
            WorkflowNode(id="output", node_type=NodeType.OUTPUT, title="Output"),
        ],
        edges=[
            WorkflowEdge(
                id="e1", from_node="input", to_node="analyze", condition=EdgeCondition.SUCCESS
            ),
            WorkflowEdge(
                id="e2", from_node="analyze", to_node="output", condition=EdgeCondition.SUCCESS
            ),
        ],
    )


@pytest_asyncio.fixture
async def runtime_database(tmp_path) -> Database:
    database = Database(tmp_path / "data" / "agent-hub.db")
    await database.initialize()
    return database
