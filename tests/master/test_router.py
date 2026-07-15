"""Tests for capability-based deterministic agent routing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from master.router import (
    AgentCapability,
    AgentCatalog,
    AgentRouter,
    AgentSpec,
    RoutingDecision,
    RoutingResult,
)
from protocol import AssignmentMode, NodeType, RiskLevel, TaskKind, WorkflowNode

ALL_CAPABILITIES = frozenset(AgentCapability)


def _agent(
    agent_id: str,
    *,
    adapter_type: str = "opencode",
    capabilities: frozenset[AgentCapability] = ALL_CAPABILITIES,
    enabled: bool = True,
    available: bool = True,
    auto_assignable: bool = True,
    hash_character: str = "a",
) -> AgentSpec:
    return AgentSpec(
        agent_id=agent_id,
        display_name=agent_id,
        adapter_type=adapter_type,
        spec_sha256=hash_character * 64,
        capabilities=capabilities,
        enabled=enabled,
        available=available,
        auto_assignable=auto_assignable,
        unavailable_reason="probe failed" if not available else None,
    )


@pytest.fixture
def sample_catalog() -> AgentCatalog:
    return AgentCatalog(
        agents=(
            _agent("agent-zeta", hash_character="e"),
            _agent("agent-opencode-1", hash_character="a"),
            _agent(
                "agent-mock-1",
                adapter_type="mock",
                auto_assignable=False,
                hash_character="b",
            ),
            _agent("agent-disabled-1", enabled=False, hash_character="c"),
            _agent("agent-offline-1", available=False, hash_character="d"),
        ),
        catalog_hash="0" * 64,
    )


def _node(
    *,
    node_type: NodeType = NodeType.AGENT_TASK,
    task_kind: TaskKind | None = TaskKind.ANALYZE,
    requires_write: bool = False,
    risk: RiskLevel = RiskLevel.L1,
    mode: AssignmentMode = AssignmentMode.AUTO,
    assigned_agent: str | None = None,
) -> WorkflowNode:
    return WorkflowNode(
        id="task-1",
        node_type=node_type,
        task_kind=task_kind,
        title="Test node",
        assignment_mode=mode,
        assigned_agent=assigned_agent,
        risk_level_hint=risk,
        requires_write=requires_write,
        system_managed=False,
    )


def test_auto_write_scores_capability_fit(sample_catalog: AgentCatalog) -> None:
    result = AgentRouter(sample_catalog).route(
        _node(
            task_kind=TaskKind.IMPLEMENT,
            requires_write=True,
            risk=RiskLevel.L2,
        )
    )

    assert result.decision == RoutingDecision.ASSIGNED
    assert result.resolved_agent_id == "agent-opencode-1"
    assert result.resolved_agent_spec_sha256 == "a" * 64
    assert result.score == 100
    assert [candidate.agent_id for candidate in result.ranked_candidates] == [
        "agent-opencode-1",
        "agent-zeta",
    ]
    assert "generate_patch" in result.reason
    assert "elevated-risk review capability bonus" in result.reason


def test_auto_is_independent_of_catalog_order(sample_catalog: AgentCatalog) -> None:
    reversed_catalog = AgentCatalog(
        agents=tuple(reversed(sample_catalog.agents)),
        catalog_hash=sample_catalog.catalog_hash,
    )
    node = _node(task_kind=TaskKind.REVIEW)

    assert AgentRouter(sample_catalog).route(node) == AgentRouter(reversed_catalog).route(node)


def test_auto_blocks_when_capabilities_are_missing() -> None:
    catalog = AgentCatalog(
        agents=(
            _agent(
                "read-only-agent",
                capabilities=frozenset({AgentCapability.READ_CODE, AgentCapability.IMPLEMENT}),
            ),
        ),
        catalog_hash="0" * 64,
    )

    result = AgentRouter(catalog).route(_node(task_kind=TaskKind.IMPLEMENT, requires_write=True))

    assert result.decision == RoutingDecision.BLOCKED_UNAVAILABLE
    assert result.resolved_agent_id is None
    assert "generate_patch" in result.blocked_reason
    assert "write_files" in result.blocked_reason


def test_elevated_risk_capability_is_a_bonus_not_a_gate() -> None:
    capabilities = ALL_CAPABILITIES.difference({AgentCapability.RISK_REVIEW})
    catalog = AgentCatalog(
        agents=(_agent("agent-opencode-1", capabilities=capabilities),),
        catalog_hash="0" * 64,
    )

    result = AgentRouter(catalog).route(
        _node(task_kind=TaskKind.IMPLEMENT, requires_write=True, risk=RiskLevel.L2)
    )

    assert result.decision == RoutingDecision.ASSIGNED
    assert result.score == 90
    assert "bonus" not in result.reason


def test_mock_is_never_silently_auto_assigned() -> None:
    catalog = AgentCatalog(
        agents=(
            _agent(
                "agent-mock-1",
                adapter_type="mock",
                auto_assignable=False,
            ),
        ),
        catalog_hash="0" * 64,
    )

    result = AgentRouter(catalog).route(_node())

    assert result.decision == RoutingDecision.BLOCKED_UNAVAILABLE
    assert "auto-assignable" in result.blocked_reason


def test_manual_mock_assignment_is_preserved(sample_catalog: AgentCatalog) -> None:
    result = AgentRouter(sample_catalog).route(
        _node(mode=AssignmentMode.MANUAL, assigned_agent="agent-mock-1")
    )

    assert result.decision == RoutingDecision.ASSIGNED
    assert result.resolved_agent_id == "agent-mock-1"
    assert result.reason == "preserved manual assignment"


def test_locked_assignment_is_preserved(sample_catalog: AgentCatalog) -> None:
    result = AgentRouter(sample_catalog).route(
        _node(mode=AssignmentMode.LOCKED, assigned_agent="agent-zeta")
    )

    assert result.decision == RoutingDecision.ASSIGNED
    assert result.resolved_agent_id == "agent-zeta"
    assert result.reason == "preserved locked assignment"


@pytest.mark.parametrize("mode", [AssignmentMode.MANUAL, AssignmentMode.LOCKED])
def test_explicit_assignment_requires_agent(
    sample_catalog: AgentCatalog,
    mode: AssignmentMode,
) -> None:
    result = AgentRouter(sample_catalog).route(_node(mode=mode))
    assert result.decision == RoutingDecision.BLOCKED_UNAVAILABLE
    assert f"{mode.value} assignment requires assigned_agent" == result.blocked_reason


@pytest.mark.parametrize(
    ("agent_id", "message"),
    [
        ("missing-agent", "not in catalog"),
        ("agent-disabled-1", "is disabled"),
        ("agent-offline-1", "is unavailable: probe failed"),
    ],
)
def test_explicit_unavailable_agent_has_reason(
    sample_catalog: AgentCatalog,
    agent_id: str,
    message: str,
) -> None:
    result = AgentRouter(sample_catalog).route(
        _node(mode=AssignmentMode.LOCKED, assigned_agent=agent_id)
    )
    assert result.decision == RoutingDecision.BLOCKED_UNAVAILABLE
    assert message in result.blocked_reason


@pytest.mark.parametrize(
    "node_type",
    [
        NodeType.INPUT,
        NodeType.OUTPUT,
        NodeType.TEST,
        NodeType.APPROVAL,
        NodeType.MERGE_PATCH,
    ],
)
def test_non_agent_nodes_are_master_managed(
    sample_catalog: AgentCatalog,
    node_type: NodeType,
) -> None:
    result = AgentRouter(sample_catalog).route(_node(node_type=node_type, task_kind=None))

    assert result.decision == RoutingDecision.NOT_APPLICABLE
    assert result.resolved_agent_id is None
    assert "executed by Master" in result.reason


def test_auto_agent_task_requires_task_kind(sample_catalog: AgentCatalog) -> None:
    result = AgentRouter(sample_catalog).route(_node(task_kind=None))
    assert result.decision == RoutingDecision.BLOCKED_UNAVAILABLE
    assert result.blocked_reason == "auto agent_task requires task_kind"


def test_catalog_lookup_is_deterministic(sample_catalog: AgentCatalog) -> None:
    assert sample_catalog.find_by_id("agent-opencode-1").spec_sha256 == "a" * 64
    assert sample_catalog.find_by_id("missing") is None
    assert sample_catalog.find_available_by_type("opencode").agent_id == "agent-opencode-1"


def test_duplicate_agent_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate agent_id"):
        AgentCatalog(
            agents=(_agent("duplicate"), _agent("duplicate", hash_character="b")),
            catalog_hash="0" * 64,
        )


def test_non_assigned_result_rejects_partial_agent_identity() -> None:
    with pytest.raises(ValidationError, match="cannot carry an agent identity"):
        RoutingResult(
            decision=RoutingDecision.NOT_APPLICABLE,
            resolved_agent_id="agent-opencode-1",
        )
