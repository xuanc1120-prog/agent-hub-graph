"""Deterministic agent assignment from an immutable catalog snapshot."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from protocol import (
    AssignmentMode,
    EntityId,
    FrozenStrictModel,
    NodeType,
    RiskLevel,
    Sha256Hex,
    TaskKind,
    WorkflowNode,
)

AdapterType = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")]
DisplayName = Annotated[str, Field(min_length=1, max_length=200)]
RoutingReason = Annotated[str, Field(min_length=1, max_length=1_000)]


class AgentCapability(StrEnum):
    READ_CODE = "read_code"
    ANALYZE = "analyze"
    IMPLEMENT = "implement"
    WRITE_FILES = "write_files"
    GENERATE_PATCH = "generate_patch"
    RUN_TESTS = "run_tests"
    REVIEW = "review"
    DOCS = "docs"
    RISK_REVIEW = "risk_review"


class RoutingDecision(StrEnum):
    ASSIGNED = "assigned"
    BLOCKED_UNAVAILABLE = "blocked_unavailable"
    NOT_APPLICABLE = "not_applicable"


class AgentSpec(FrozenStrictModel):
    """Agent identity and capability snapshot used by one compilation."""

    agent_id: EntityId
    display_name: DisplayName
    adapter_type: AdapterType
    spec_sha256: Sha256Hex
    capabilities: frozenset[AgentCapability] = Field(default_factory=frozenset, max_length=32)
    enabled: bool = True
    available: bool = True
    auto_assignable: bool = True
    unavailable_reason: str | None = Field(default=None, max_length=500)

    @property
    def can_run(self) -> bool:
        return self.enabled and self.available


class AgentCatalog(FrozenStrictModel):
    """Immutable catalog; ordering never changes routing outcomes."""

    agents: tuple[AgentSpec, ...] = Field(default_factory=tuple, max_length=100)
    catalog_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_unique_agent_ids(self) -> Self:
        ids = [agent.agent_id for agent in self.agents]
        if len(ids) != len(set(ids)):
            raise ValueError("agent catalog contains duplicate agent_id values")
        return self

    def find_by_id(self, agent_id: str) -> AgentSpec | None:
        return next((agent for agent in self.agents if agent.agent_id == agent_id), None)

    def find_available_by_type(self, adapter_type: str) -> AgentSpec | None:
        matches = sorted(
            (
                agent
                for agent in self.agents
                if agent.adapter_type == adapter_type and agent.can_run
            ),
            key=lambda agent: agent.agent_id,
        )
        return matches[0] if matches else None


class AgentScore(FrozenStrictModel):
    agent_id: EntityId
    agent_spec_sha256: Sha256Hex
    score: int = Field(ge=0, le=100)
    reason: RoutingReason


class RoutingResult(FrozenStrictModel):
    """One node's compile-time routing decision and auditable ranking."""

    decision: RoutingDecision
    resolved_agent_id: EntityId | None = None
    resolved_agent_spec_sha256: Sha256Hex | None = None
    score: int | None = Field(default=None, ge=0, le=100)
    reason: str | None = Field(default=None, max_length=1_000)
    blocked_reason: str | None = Field(default=None, max_length=1_000)
    ranked_candidates: tuple[AgentScore, ...] = Field(default_factory=tuple, max_length=100)

    @model_validator(mode="after")
    def validate_decision_shape(self) -> Self:
        has_complete_identity = (
            self.resolved_agent_id is not None and self.resolved_agent_spec_sha256 is not None
        )
        has_any_identity = (
            self.resolved_agent_id is not None or self.resolved_agent_spec_sha256 is not None
        )
        if self.decision == RoutingDecision.ASSIGNED and not has_complete_identity:
            raise ValueError("assigned routing result requires an agent identity")
        if self.decision != RoutingDecision.ASSIGNED and has_any_identity:
            raise ValueError("non-assigned routing result cannot carry an agent identity")
        if self.decision == RoutingDecision.BLOCKED_UNAVAILABLE and not self.blocked_reason:
            raise ValueError("blocked routing result requires blocked_reason")
        return self


_TASK_CAPABILITY = {
    TaskKind.ANALYZE: AgentCapability.ANALYZE,
    TaskKind.IMPLEMENT: AgentCapability.IMPLEMENT,
    TaskKind.REVIEW: AgentCapability.REVIEW,
    TaskKind.DOCS: AgentCapability.DOCS,
    TaskKind.TEST_FIX: AgentCapability.RUN_TESTS,
}

_CAPABILITY_WEIGHT = {
    AgentCapability.READ_CODE: 10,
    AgentCapability.ANALYZE: 15,
    AgentCapability.IMPLEMENT: 20,
    AgentCapability.WRITE_FILES: 20,
    AgentCapability.GENERATE_PATCH: 20,
    AgentCapability.RUN_TESTS: 15,
    AgentCapability.REVIEW: 15,
    AgentCapability.DOCS: 15,
    AgentCapability.RISK_REVIEW: 10,
}


class AgentRouter:
    """Resolve agent_task nodes without mutating the AuthorGraph."""

    def __init__(self, catalog: AgentCatalog) -> None:
        self._catalog = catalog

    def route(self, node: WorkflowNode) -> RoutingResult:
        if node.node_type != NodeType.AGENT_TASK:
            return RoutingResult(
                decision=RoutingDecision.NOT_APPLICABLE,
                reason=f"{node.node_type.value} nodes are executed by Master",
            )

        if node.assignment_mode == AssignmentMode.AUTO:
            return self._route_auto(node)
        if node.assignment_mode in {AssignmentMode.MANUAL, AssignmentMode.LOCKED}:
            return self._route_explicit(node)
        return RoutingResult(
            decision=RoutingDecision.BLOCKED_UNAVAILABLE,
            blocked_reason=f"unknown assignment mode: {node.assignment_mode}",
        )

    def _route_auto(self, node: WorkflowNode) -> RoutingResult:
        if node.task_kind is None:
            return RoutingResult(
                decision=RoutingDecision.BLOCKED_UNAVAILABLE,
                blocked_reason="auto agent_task requires task_kind",
            )

        requirements = self._requirements(node)
        ranked: list[AgentScore] = []
        for agent in self._catalog.agents:
            if not agent.can_run or not agent.auto_assignable:
                continue
            missing = requirements.difference(agent.capabilities)
            if missing:
                continue
            score = 20 + sum(_CAPABILITY_WEIGHT[item] for item in requirements)
            reason = "matches required capabilities: " + ", ".join(
                capability.value for capability in sorted(requirements, key=lambda item: item.value)
            )
            if (
                node.risk_level_hint in {RiskLevel.L2, RiskLevel.L3, RiskLevel.L4}
                and AgentCapability.RISK_REVIEW in agent.capabilities
            ):
                score += _CAPABILITY_WEIGHT[AgentCapability.RISK_REVIEW]
                reason += "; elevated-risk review capability bonus"
            ranked.append(
                AgentScore(
                    agent_id=agent.agent_id,
                    agent_spec_sha256=agent.spec_sha256,
                    score=min(100, score),
                    reason=reason,
                )
            )

        ranked.sort(key=lambda candidate: (-candidate.score, candidate.agent_id))
        if not ranked:
            required = ", ".join(
                capability.value for capability in sorted(requirements, key=lambda item: item.value)
            )
            return RoutingResult(
                decision=RoutingDecision.BLOCKED_UNAVAILABLE,
                blocked_reason=(
                    "no enabled, available, auto-assignable agent satisfies required "
                    f"capabilities: {required}"
                ),
            )

        selected = ranked[0]
        return RoutingResult(
            decision=RoutingDecision.ASSIGNED,
            resolved_agent_id=selected.agent_id,
            resolved_agent_spec_sha256=selected.agent_spec_sha256,
            score=selected.score,
            reason=selected.reason,
            ranked_candidates=tuple(ranked),
        )

    def _route_explicit(self, node: WorkflowNode) -> RoutingResult:
        mode = node.assignment_mode.value
        if node.assigned_agent is None:
            return RoutingResult(
                decision=RoutingDecision.BLOCKED_UNAVAILABLE,
                blocked_reason=f"{mode} assignment requires assigned_agent",
            )

        agent = self._catalog.find_by_id(node.assigned_agent)
        if agent is None:
            return RoutingResult(
                decision=RoutingDecision.BLOCKED_UNAVAILABLE,
                blocked_reason=f"assigned agent {node.assigned_agent} not in catalog",
            )
        if not agent.enabled:
            return RoutingResult(
                decision=RoutingDecision.BLOCKED_UNAVAILABLE,
                blocked_reason=f"assigned agent {agent.agent_id} is disabled",
            )
        if not agent.available:
            detail = agent.unavailable_reason or "availability probe failed"
            return RoutingResult(
                decision=RoutingDecision.BLOCKED_UNAVAILABLE,
                blocked_reason=f"assigned agent {agent.agent_id} is unavailable: {detail}",
            )
        return RoutingResult(
            decision=RoutingDecision.ASSIGNED,
            resolved_agent_id=agent.agent_id,
            resolved_agent_spec_sha256=agent.spec_sha256,
            reason=f"preserved {mode} assignment",
        )

    @staticmethod
    def _requirements(node: WorkflowNode) -> frozenset[AgentCapability]:
        assert node.task_kind is not None
        requirements = {
            AgentCapability.READ_CODE,
            _TASK_CAPABILITY[node.task_kind],
        }
        if node.requires_write:
            requirements.update({AgentCapability.WRITE_FILES, AgentCapability.GENERATE_PATCH})
        return frozenset(requirements)
