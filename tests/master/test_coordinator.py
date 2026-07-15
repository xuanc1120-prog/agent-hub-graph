"""Tests for planner fallback evidence and immutable workflow lineage."""

from __future__ import annotations

import asyncio
from json import JSONDecodeError
from typing import cast

import pytest
from pydantic import ValidationError

from context.planner_bundle import PlannerContextBundle
from master.coordinator import (
    ParentWorkflowSnapshot,
    PlannerCoordinator,
    PlannerFailureCode,
    build_workflow_lineage,
)
from master.planner import BasePlanner, PlannerInput, PlannerOutput, RuleBasedPlanner
from protocol import PlannerRunStatus, PlannerType, WorkflowDraft

BASE_COMMIT = "a" * 40


def _input(goal: str = "Fix the login bug") -> PlannerInput:
    return PlannerInput(
        context_bundle=PlannerContextBundle.create(
            session_id="session-1",
            goal=goal,
            integration_base_commit=BASE_COMMIT,
        )
    )


class SuccessfulOpenCodePlanner(BasePlanner):
    @property
    def planner_id(self) -> str:
        return "opencode-planner"

    @property
    def planner_type(self) -> PlannerType:
        return PlannerType.OPEN_CODE

    async def plan(self, planner_input: PlannerInput) -> PlannerOutput:
        rule_output = await RuleBasedPlanner(planner_id=self.planner_id).plan(planner_input)
        draft_payload = rule_output.draft.model_dump(mode="python")
        draft_payload["planner_type"] = PlannerType.OPEN_CODE
        draft = WorkflowDraft.model_validate(draft_payload)
        return PlannerOutput(
            draft=draft,
            planner_type=self.planner_type,
            context_bundle_sha256=planner_input.context_bundle.bundle_hash,
        )


class FailingOpenCodePlanner(BasePlanner):
    @property
    def planner_id(self) -> str:
        return "opencode-planner"

    @property
    def planner_type(self) -> PlannerType:
        return PlannerType.OPEN_CODE

    async def plan(self, planner_input: PlannerInput) -> PlannerOutput:
        raise RuntimeError("OpenCode planner unavailable")


class SlowOpenCodePlanner(FailingOpenCodePlanner):
    async def plan(self, planner_input: PlannerInput) -> PlannerOutput:
        await asyncio.sleep(60)
        raise AssertionError("unreachable")


class InvalidOpenCodePlanner(FailingOpenCodePlanner):
    async def plan(self, planner_input: PlannerInput) -> PlannerOutput:
        return cast(PlannerOutput, {"unexpected": "mapping"})


class InvalidJsonOpenCodePlanner(FailingOpenCodePlanner):
    async def plan(self, planner_input: PlannerInput) -> PlannerOutput:
        raise JSONDecodeError("invalid planner JSON", "{", 1)


class FailingRulePlanner(BasePlanner):
    @property
    def planner_id(self) -> str:
        return "rule-based-planner"

    @property
    def planner_type(self) -> PlannerType:
        return PlannerType.RULE_BASED

    async def plan(self, planner_input: PlannerInput) -> PlannerOutput:
        raise RuntimeError("Fallback also failed")


@pytest.mark.asyncio
async def test_primary_success_skips_fallback() -> None:
    result = await PlannerCoordinator(
        SuccessfulOpenCodePlanner(),
        RuleBasedPlanner(),
    ).plan_with_fallback(
        _input(),
        primary_run_id="run-primary",
        fallback_run_id="run-fallback",
    )

    assert result.primary_run.status == PlannerRunStatus.SUCCEEDED
    assert result.primary_run.planner_type == PlannerType.OPEN_CODE
    assert result.primary_run.result_draft_sha256 is not None
    assert result.fallback_run is None
    assert result.final_output is not None
    assert result.final_output.planner_type == PlannerType.OPEN_CODE


@pytest.mark.asyncio
async def test_primary_failure_creates_distinct_fallback_evidence() -> None:
    planner_input = _input()
    result = await PlannerCoordinator(
        FailingOpenCodePlanner(),
        RuleBasedPlanner(),
    ).plan_with_fallback(
        planner_input,
        primary_run_id="run-primary",
        fallback_run_id="run-fallback",
    )

    assert result.primary_run.planner_run_id == "run-primary"
    assert result.primary_run.status == PlannerRunStatus.FAILED
    assert result.primary_run.error_code == PlannerFailureCode.EXECUTION_FAILED
    assert result.primary_run.error_message == "OpenCode planner unavailable"
    assert result.primary_run.result_draft_sha256 is None

    assert result.fallback_run is not None
    assert result.fallback_run.planner_run_id == "run-fallback"
    assert result.fallback_run.status == PlannerRunStatus.SUCCEEDED
    assert result.fallback_run.fallback_from_run_id == "run-primary"
    assert result.fallback_run.context_bundle_sha256 == result.primary_run.context_bundle_sha256
    assert result.fallback_run.result_draft_sha256 is not None
    assert result.final_output is not None
    assert result.final_output.planner_type == PlannerType.RULE_BASED
    assert result.final_output.context_bundle_sha256 == planner_input.context_bundle.bundle_hash


@pytest.mark.asyncio
async def test_unavailable_primary_uses_fallback() -> None:
    result = await PlannerCoordinator(None, RuleBasedPlanner()).plan_with_fallback(
        _input("Add a profile page"),
        primary_run_id="run-primary",
        fallback_run_id="run-fallback",
    )

    assert result.primary_run.status == PlannerRunStatus.FAILED
    assert result.primary_run.error_code == PlannerFailureCode.UNAVAILABLE
    assert result.fallback_run is not None
    assert result.fallback_run.status == PlannerRunStatus.SUCCEEDED
    assert result.final_output is not None


@pytest.mark.asyncio
async def test_primary_timeout_is_distinct_from_failure() -> None:
    result = await PlannerCoordinator(
        SlowOpenCodePlanner(),
        RuleBasedPlanner(),
        primary_timeout_seconds=0.001,
    ).plan_with_fallback(
        _input(),
        primary_run_id="run-primary",
        fallback_run_id="run-fallback",
    )

    assert result.primary_run.status == PlannerRunStatus.TIMED_OUT
    assert result.primary_run.error_code == PlannerFailureCode.TIMED_OUT
    assert result.fallback_run is not None
    assert result.fallback_run.status == PlannerRunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_invalid_structured_output_triggers_fallback() -> None:
    result = await PlannerCoordinator(
        InvalidOpenCodePlanner(),
        RuleBasedPlanner(),
    ).plan_with_fallback(
        _input(),
        primary_run_id="run-primary",
        fallback_run_id="run-fallback",
    )

    assert result.primary_run.status == PlannerRunStatus.FAILED
    assert result.primary_run.error_code == PlannerFailureCode.INVALID_OUTPUT
    assert result.primary_run.error_message == "planner did not return PlannerOutput"
    assert result.fallback_run is not None
    assert result.fallback_run.status == PlannerRunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_adapter_json_parse_error_is_invalid_output() -> None:
    result = await PlannerCoordinator(
        InvalidJsonOpenCodePlanner(),
        RuleBasedPlanner(),
    ).plan_with_fallback(
        _input(),
        primary_run_id="run-primary",
        fallback_run_id="run-fallback",
    )

    assert result.primary_run.error_code == PlannerFailureCode.INVALID_OUTPUT
    assert result.fallback_run is not None
    assert result.fallback_run.status == PlannerRunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_fallback_failure_preserves_both_attempts() -> None:
    result = await PlannerCoordinator(
        FailingOpenCodePlanner(),
        FailingRulePlanner(),
    ).plan_with_fallback(
        _input(),
        primary_run_id="run-primary",
        fallback_run_id="run-fallback",
    )

    assert result.primary_run.error_message == "OpenCode planner unavailable"
    assert result.fallback_run is not None
    assert result.fallback_run.status == PlannerRunStatus.FAILED
    assert result.fallback_run.error_message == "Fallback also failed"
    assert result.fallback_run.fallback_from_run_id == "run-primary"
    assert result.final_output is None


@pytest.mark.asyncio
async def test_fallback_run_id_is_required_after_failure() -> None:
    coordinator = PlannerCoordinator(FailingOpenCodePlanner(), RuleBasedPlanner())
    with pytest.raises(ValueError, match="fallback_run_id required"):
        await coordinator.plan_with_fallback(_input(), primary_run_id="run-primary")


@pytest.mark.asyncio
async def test_primary_and_fallback_ids_must_be_distinct() -> None:
    coordinator = PlannerCoordinator(FailingOpenCodePlanner(), RuleBasedPlanner())
    with pytest.raises(ValueError, match="must be distinct"):
        await coordinator.plan_with_fallback(
            _input(),
            primary_run_id="same-run",
            fallback_run_id="same-run",
        )


class SecretFailurePlanner(FailingOpenCodePlanner):
    async def plan(self, planner_input: PlannerInput) -> PlannerOutput:
        raise RuntimeError("provider token=secret-value-123456789 rejected")


@pytest.mark.asyncio
async def test_failure_message_is_bounded_and_redacted() -> None:
    result = await PlannerCoordinator(
        SecretFailurePlanner(),
        RuleBasedPlanner(),
    ).plan_with_fallback(
        _input(),
        primary_run_id="run-primary",
        fallback_run_id="run-fallback",
    )
    assert "secret-value" not in result.primary_run.error_message
    assert "[REDACTED]" in result.primary_run.error_message


def _parent(session_id: str = "session-1") -> ParentWorkflowSnapshot:
    return ParentWorkflowSnapshot(
        workflow_id="workflow-parent",
        session_id=session_id,
        semantic_version=7,
        author_graph_sha256="b" * 64,
    )


def test_replan_creates_child_lineage_without_mutating_parent() -> None:
    parent = _parent()
    before = parent.model_dump(mode="json")

    lineage = build_workflow_lineage(
        workflow_id="workflow-child",
        session_id="session-1",
        source_planner_run_id="run-fallback",
        parent=parent,
    )

    assert lineage.workflow_id == "workflow-child"
    assert lineage.parent_workflow_id == "workflow-parent"
    assert lineage.parent_semantic_version == 7
    assert lineage.parent_author_graph_sha256 == "b" * 64
    assert lineage.initial_semantic_version == 1
    assert parent.model_dump(mode="json") == before
    with pytest.raises(ValidationError, match="frozen"):
        parent.semantic_version = 8


def test_cross_session_parent_is_rejected() -> None:
    with pytest.raises(ValueError, match="same session"):
        build_workflow_lineage(
            workflow_id="workflow-child",
            session_id="session-1",
            source_planner_run_id="run-fallback",
            parent=_parent("session-2"),
        )


def test_replan_cannot_reuse_parent_identity() -> None:
    with pytest.raises(ValueError, match="create a new workflow"):
        build_workflow_lineage(
            workflow_id="workflow-parent",
            session_id="session-1",
            source_planner_run_id="run-fallback",
            parent=_parent(),
        )


def test_first_workflow_has_no_parent_lineage() -> None:
    lineage = build_workflow_lineage(
        workflow_id="workflow-first",
        session_id="session-1",
        source_planner_run_id="run-primary",
    )
    assert lineage.parent_workflow_id is None
    assert lineage.parent_semantic_version is None
    assert lineage.initial_semantic_version == 1
