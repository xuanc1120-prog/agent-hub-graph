"""Deterministic workflow planning with bounded, non-executable inputs."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from pydantic import TypeAdapter, model_validator

from context.planner_bundle import PlannerContextBundle
from protocol import (
    AssignmentMode,
    EdgeCondition,
    EntityId,
    FrozenStrictModel,
    NodeType,
    PlannerType,
    RiskLevel,
    Sha256Hex,
    TaskKind,
    WorkflowDraft,
    WorkflowEdge,
    WorkflowNode,
)

_ENTITY_ID = TypeAdapter(EntityId)


class TemplateKind(StrEnum):
    """Supported deterministic workflow families."""

    BUGFIX = "bugfix"
    FEATURE = "feature"
    REFACTOR = "refactor"
    DOCS = "docs"


class UnsupportedTaskFamily(ValueError):
    """Raised when no deterministic template can safely represent a goal."""


class PlannerInput(FrozenStrictModel):
    """Planner request containing only a verified context bundle and intent."""

    context_bundle: PlannerContextBundle
    task_family: TemplateKind | None = None


class PlannerOutput(FrozenStrictModel):
    """Structured planner result; it is a draft and never an execution command."""

    draft: WorkflowDraft
    planner_type: PlannerType
    context_bundle_sha256: Sha256Hex
    template_id: EntityId | None = None

    @model_validator(mode="after")
    def validate_planner_identity(self) -> Self:
        if self.draft.planner_type != self.planner_type:
            raise ValueError("draft planner_type does not match output planner_type")
        return self


class BasePlanner(ABC):
    """Planner boundary. Implementations may only return ``PlannerOutput``."""

    @property
    @abstractmethod
    def planner_id(self) -> EntityId:
        raise NotImplementedError

    @property
    @abstractmethod
    def planner_type(self) -> PlannerType:
        raise NotImplementedError

    @abstractmethod
    async def plan(self, planner_input: PlannerInput) -> PlannerOutput:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class _TemplateStep:
    node_id: str
    title: str
    instruction: str | None
    task_kind: TaskKind | None
    risk: RiskLevel
    requires_write: bool = False


_TEMPLATES: dict[TemplateKind, tuple[_TemplateStep, ...]] = {
    TemplateKind.BUGFIX: (
        _TemplateStep(
            "input-1",
            "Accept bug report",
            "Collect the bug description, evidence, and reproduction steps.",
            None,
            RiskLevel.L0,
        ),
        _TemplateStep(
            "investigate-1",
            "Investigate root cause",
            "Analyze the relevant code and evidence to identify the root cause.",
            TaskKind.ANALYZE,
            RiskLevel.L1,
        ),
        _TemplateStep(
            "fix-1",
            "Implement fix",
            "Implement the smallest change that addresses the verified root cause.",
            TaskKind.IMPLEMENT,
            RiskLevel.L2,
            True,
        ),
        _TemplateStep(
            "test-1",
            "Add regression coverage",
            "Verify the fix and add focused regression coverage when needed.",
            TaskKind.TEST_FIX,
            RiskLevel.L2,
            True,
        ),
        _TemplateStep(
            "output-1",
            "Summarize fix and verification",
            None,
            None,
            RiskLevel.L0,
        ),
    ),
    TemplateKind.FEATURE: (
        _TemplateStep(
            "input-1",
            "Accept feature requirements",
            "Collect the feature scope, constraints, and acceptance criteria.",
            None,
            RiskLevel.L0,
        ),
        _TemplateStep(
            "design-1",
            "Design implementation approach",
            "Analyze the codebase and define a bounded implementation approach.",
            TaskKind.ANALYZE,
            RiskLevel.L1,
        ),
        _TemplateStep(
            "implement-1",
            "Implement feature",
            "Implement the accepted feature scope and preserve existing contracts.",
            TaskKind.IMPLEMENT,
            RiskLevel.L2,
            True,
        ),
        _TemplateStep(
            "test-1",
            "Verify feature",
            "Add or update focused tests for the feature acceptance criteria.",
            TaskKind.TEST_FIX,
            RiskLevel.L2,
            True,
        ),
        _TemplateStep(
            "output-1",
            "Summarize feature implementation",
            None,
            None,
            RiskLevel.L0,
        ),
    ),
    TemplateKind.REFACTOR: (
        _TemplateStep(
            "input-1",
            "Accept refactoring scope",
            "Collect the refactoring boundary and behavior-preservation constraints.",
            None,
            RiskLevel.L0,
        ),
        _TemplateStep(
            "analyze-1",
            "Analyze current structure",
            "Identify structural problems and the behavior that must remain unchanged.",
            TaskKind.ANALYZE,
            RiskLevel.L1,
        ),
        _TemplateStep(
            "refactor-1",
            "Apply refactoring",
            "Refactor within the accepted boundary while preserving behavior.",
            TaskKind.IMPLEMENT,
            RiskLevel.L2,
            True,
        ),
        _TemplateStep(
            "verify-1",
            "Verify preserved behavior",
            "Run focused verification and repair regressions introduced by the refactor.",
            TaskKind.TEST_FIX,
            RiskLevel.L2,
            True,
        ),
        _TemplateStep(
            "output-1",
            "Summarize refactoring changes",
            None,
            None,
            RiskLevel.L0,
        ),
    ),
    TemplateKind.DOCS: (
        _TemplateStep(
            "input-1",
            "Accept documentation scope",
            "Collect the audience, source material, and documentation boundary.",
            None,
            RiskLevel.L0,
        ),
        _TemplateStep(
            "review-1",
            "Review source material",
            "Review the relevant code and existing documentation for accuracy gaps.",
            TaskKind.DOCS,
            RiskLevel.L1,
        ),
        _TemplateStep(
            "document-1",
            "Write documentation",
            "Update documentation with accurate, scoped, and verifiable content.",
            TaskKind.DOCS,
            RiskLevel.L1,
            True,
        ),
        _TemplateStep(
            "output-1",
            "Summarize documentation changes",
            None,
            None,
            RiskLevel.L0,
        ),
    ),
}

_INFERENCE_RULES = (
    (
        TemplateKind.BUGFIX,
        re.compile(r"\b(?:bug|bugs|bugfix|fix|repair|error|crash|broken|regression)\b"),
        ("修复", "错误", "崩溃", "故障"),
    ),
    (
        TemplateKind.DOCS,
        re.compile(
            r"\b(?:doc|docs|document|documentation|readme|comment|comments|explain|guide)\b"
        ),
        ("文档", "说明", "注释"),
    ),
    (
        TemplateKind.REFACTOR,
        re.compile(r"\b(?:refactor|cleanup|reorganize|simplify)\b"),
        ("重构", "整理", "简化"),
    ),
    (
        TemplateKind.FEATURE,
        re.compile(r"\b(?:add|implement|create|build|feature|support|improve)\b"),
        ("新增", "添加", "实现", "创建", "支持", "改进"),
    ),
)


class RuleBasedPlanner(BasePlanner):
    """Reliable fallback using four stable workflow templates."""

    def __init__(self, planner_id: str = "rule-based-planner") -> None:
        self._planner_id = _ENTITY_ID.validate_python(planner_id)

    @property
    def planner_id(self) -> EntityId:
        return self._planner_id

    @property
    def planner_type(self) -> PlannerType:
        return PlannerType.RULE_BASED

    async def plan(self, planner_input: PlannerInput) -> PlannerOutput:
        template_kind = planner_input.task_family or self._infer_template(
            planner_input.context_bundle.goal
        )
        draft = self._build_draft(planner_input.context_bundle, template_kind)
        return PlannerOutput(
            draft=draft,
            planner_type=self.planner_type,
            context_bundle_sha256=planner_input.context_bundle.bundle_hash,
            template_id=f"rulebase-v1-{template_kind.value}",
        )

    @staticmethod
    def _infer_template(goal: str) -> TemplateKind:
        normalized = goal.casefold()
        for family, word_pattern, cjk_keywords in _INFERENCE_RULES:
            if word_pattern.search(normalized) or any(
                keyword in normalized for keyword in cjk_keywords
            ):
                return family
        raise UnsupportedTaskFamily(
            "cannot infer task family; specify one of bugfix, feature, refactor, or docs"
        )

    def _build_draft(
        self,
        context_bundle: PlannerContextBundle,
        template_kind: TemplateKind,
    ) -> WorkflowDraft:
        steps = _TEMPLATES[template_kind]
        nodes = [self._build_node(index, step, len(steps)) for index, step in enumerate(steps)]
        edges = [
            WorkflowEdge(
                id=f"edge-{index}",
                from_node=steps[index - 1].node_id,
                to_node=steps[index].node_id,
                condition=EdgeCondition.SUCCESS,
                system_managed=False,
            )
            for index in range(1, len(steps))
        ]
        return WorkflowDraft(
            session_id=context_bundle.session_id,
            goal=context_bundle.goal,
            planner_id=self.planner_id,
            planner_type=self.planner_type,
            nodes=nodes,
            edges=edges,
            assumptions=[f"Selected deterministic {template_kind.value} workflow template."],
            risks=[],
            required_user_inputs=[],
        )

    @staticmethod
    def _build_node(index: int, step: _TemplateStep, step_count: int) -> WorkflowNode:
        if index == 0:
            node_type = NodeType.INPUT
        elif index == step_count - 1:
            node_type = NodeType.OUTPUT
        else:
            node_type = NodeType.AGENT_TASK
        return WorkflowNode(
            id=step.node_id,
            node_type=node_type,
            task_kind=step.task_kind,
            title=step.title,
            instruction=step.instruction,
            assignment_mode=AssignmentMode.AUTO,
            risk_level_hint=step.risk,
            requires_write=step.requires_write,
            system_managed=False,
        )
