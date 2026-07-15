"""Tests for bounded planner inputs and deterministic rule templates."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from context.planner_bundle import (
    MAX_PLANNER_BYTES,
    MAX_PLANNER_FILES,
    PlannerContextBundle,
    compute_bundle_hash,
)
from master.planner import (
    PlannerInput,
    RuleBasedPlanner,
    TemplateKind,
    UnsupportedTaskFamily,
)
from protocol import NodeType, PlannerType, TaskKind

BASE_COMMIT = "a" * 40

EXPECTED_TEMPLATE_SNAPSHOTS = {
    TemplateKind.BUGFIX: (
        ("input-1", NodeType.INPUT, None, False),
        ("investigate-1", NodeType.AGENT_TASK, TaskKind.ANALYZE, False),
        ("fix-1", NodeType.AGENT_TASK, TaskKind.IMPLEMENT, True),
        ("test-1", NodeType.AGENT_TASK, TaskKind.TEST_FIX, True),
        ("output-1", NodeType.OUTPUT, None, False),
    ),
    TemplateKind.FEATURE: (
        ("input-1", NodeType.INPUT, None, False),
        ("design-1", NodeType.AGENT_TASK, TaskKind.ANALYZE, False),
        ("implement-1", NodeType.AGENT_TASK, TaskKind.IMPLEMENT, True),
        ("test-1", NodeType.AGENT_TASK, TaskKind.TEST_FIX, True),
        ("output-1", NodeType.OUTPUT, None, False),
    ),
    TemplateKind.REFACTOR: (
        ("input-1", NodeType.INPUT, None, False),
        ("analyze-1", NodeType.AGENT_TASK, TaskKind.ANALYZE, False),
        ("refactor-1", NodeType.AGENT_TASK, TaskKind.IMPLEMENT, True),
        ("verify-1", NodeType.AGENT_TASK, TaskKind.TEST_FIX, True),
        ("output-1", NodeType.OUTPUT, None, False),
    ),
    TemplateKind.DOCS: (
        ("input-1", NodeType.INPUT, None, False),
        ("review-1", NodeType.AGENT_TASK, TaskKind.DOCS, False),
        ("document-1", NodeType.AGENT_TASK, TaskKind.DOCS, True),
        ("output-1", NodeType.OUTPUT, None, False),
    ),
}


def _context(goal: str = "Fix the login crash") -> PlannerContextBundle:
    return PlannerContextBundle.create(
        session_id="session-1",
        goal=goal,
        integration_base_commit=BASE_COMMIT,
        file_count=120,
        total_size_bytes=4096,
        languages=("Python", "TypeScript"),
        test_frameworks=("pytest", "vitest"),
    )


@pytest.mark.parametrize(
    ("family", "goal"),
    [
        (TemplateKind.BUGFIX, "Fix the login crash"),
        (TemplateKind.FEATURE, "Add a profile page"),
        (TemplateKind.REFACTOR, "Refactor authentication"),
        (TemplateKind.DOCS, "Document the API"),
    ],
)
@pytest.mark.asyncio
async def test_templates_match_stable_snapshots(
    family: TemplateKind,
    goal: str,
) -> None:
    planner = RuleBasedPlanner()
    planner_input = PlannerInput(context_bundle=_context(goal), task_family=family)

    first = await planner.plan(planner_input)
    second = await planner.plan(planner_input)

    assert first == second
    assert first.template_id == f"rulebase-v1-{family.value}"
    assert first.planner_type == PlannerType.RULE_BASED
    assert first.context_bundle_sha256 == planner_input.context_bundle.bundle_hash
    assert [
        (node.id, node.node_type, node.task_kind, node.requires_write) for node in first.draft.nodes
    ] == list(EXPECTED_TEMPLATE_SNAPSHOTS[family])
    assert [(edge.id, edge.from_node, edge.to_node) for edge in first.draft.edges] == [
        (f"edge-{index}", first.draft.nodes[index - 1].id, first.draft.nodes[index].id)
        for index in range(1, len(first.draft.nodes))
    ]
    assert first.draft.nodes[-1].instruction is None


@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        ("Repair a broken login", TemplateKind.BUGFIX),
        ("Create a user profile", TemplateKind.FEATURE),
        ("Simplify the auth module", TemplateKind.REFACTOR),
        ("Write a README guide", TemplateKind.DOCS),
        ("修复登录接口崩溃", TemplateKind.BUGFIX),
        ("新增用户管理模块", TemplateKind.FEATURE),
        ("重构支付服务", TemplateKind.REFACTOR),
        ("编写 API 文档", TemplateKind.DOCS),
    ],
)
@pytest.mark.asyncio
async def test_keyword_inference_is_deterministic(goal: str, expected: TemplateKind) -> None:
    result = await RuleBasedPlanner().plan(PlannerInput(context_bundle=_context(goal)))
    assert result.template_id == f"rulebase-v1-{expected.value}"


@pytest.mark.asyncio
async def test_ambiguous_goal_requires_explicit_family() -> None:
    planner = RuleBasedPlanner()
    planner_input = PlannerInput(context_bundle=_context("Assess repository health"))

    with pytest.raises(UnsupportedTaskFamily, match="specify one of"):
        await planner.plan(planner_input)

    explicit = await planner.plan(
        PlannerInput(
            context_bundle=planner_input.context_bundle,
            task_family=TemplateKind.REFACTOR,
        )
    )
    assert explicit.template_id == "rulebase-v1-refactor"


@pytest.mark.asyncio
async def test_keyword_matching_does_not_use_substrings() -> None:
    planner = RuleBasedPlanner()
    with pytest.raises(UnsupportedTaskFamily):
        await planner.plan(PlannerInput(context_bundle=_context("Update Dockerfile fixture")))


def test_unknown_explicit_family_is_rejected() -> None:
    with pytest.raises(ValidationError, match="task_family"):
        PlannerInput(context_bundle=_context(), task_family="migration")


@pytest.mark.parametrize("goal", ["", "   ", "x" * 20_001])
def test_invalid_goal_is_rejected(goal: str) -> None:
    with pytest.raises(ValidationError):
        _context(goal)


def test_context_resource_bounds_are_enforced() -> None:
    with pytest.raises(ValidationError, match="file_count"):
        PlannerContextBundle.create(
            session_id="session-1",
            goal="Fix bug",
            integration_base_commit=BASE_COMMIT,
            file_count=MAX_PLANNER_FILES + 1,
        )
    with pytest.raises(ValidationError, match="total_size_bytes"):
        PlannerContextBundle.create(
            session_id="session-1",
            goal="Fix bug",
            integration_base_commit=BASE_COMMIT,
            total_size_bytes=MAX_PLANNER_BYTES + 1,
        )


@pytest.mark.parametrize(
    "goal",
    [
        "Fix auth with api_key=super-secret-value",
        "Use sk-abcdefghijklmnopqrstuvwxyz123456",
        "Use ghp_abcdefghijklmnopqrstuvwxyz123456",
        "Inspect -----BEGIN PRIVATE KEY----- data",
    ],
)
def test_secret_like_goal_is_rejected(goal: str) -> None:
    with pytest.raises(ValidationError, match="redact it first"):
        _context(goal)


def test_context_hash_is_stable_and_verified() -> None:
    first = _context()
    second = _context()

    assert first == second
    assert compute_bundle_hash(first) == first.bundle_hash
    assert len(first.bundle_hash) == 64
    assert not hasattr(first, "source_repo_path")
    assert not hasattr(first, "file_contents")

    payload = first.model_dump(mode="python")
    payload["bundle_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="bundle_hash"):
        PlannerContextBundle.model_validate(payload)

    unrestricted = first.model_dump(mode="python")
    unrestricted["source_repo_path"] = "C:/private/source"
    with pytest.raises(ValidationError, match="source_repo_path"):
        PlannerContextBundle.model_validate(unrestricted)


def test_context_hints_are_canonical_and_secret_checked() -> None:
    first = PlannerContextBundle.create(
        session_id="session-1",
        goal="Fix bug",
        integration_base_commit=BASE_COMMIT,
        languages=("TypeScript", "python", "Python"),
    )
    second = PlannerContextBundle.create(
        session_id="session-1",
        goal="Fix bug",
        integration_base_commit=BASE_COMMIT,
        languages=("Python", "TypeScript", "python"),
    )
    assert first.languages == ("Python", "TypeScript")
    assert first == second

    with pytest.raises(ValidationError, match="redact it first"):
        PlannerContextBundle.create(
            session_id="session-1",
            goal="Fix bug",
            integration_base_commit=BASE_COMMIT,
            test_frameworks=("ghp_abcdefghijklmnopqrstuvwxyz123456",),
        )


@pytest.mark.asyncio
async def test_same_family_has_stable_topology_across_sessions() -> None:
    planner = RuleBasedPlanner()
    first = await planner.plan(
        PlannerInput(
            context_bundle=_context("Fix bug"),
            task_family=TemplateKind.BUGFIX,
        )
    )
    other_context = PlannerContextBundle.create(
        session_id="session-2",
        goal="Fix error",
        integration_base_commit="b" * 40,
    )
    second = await planner.plan(
        PlannerInput(context_bundle=other_context, task_family=TemplateKind.BUGFIX)
    )

    assert [node.id for node in first.draft.nodes] == [node.id for node in second.draft.nodes]
    assert [edge.model_dump() for edge in first.draft.edges] == [
        edge.model_dump() for edge in second.draft.edges
    ]
