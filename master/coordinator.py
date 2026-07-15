"""Planner fallback coordination and immutable workflow lineage metadata."""

from __future__ import annotations

import asyncio
import re
from enum import StrEnum
from hashlib import sha256
from json import JSONDecodeError
from typing import Annotated, Literal, Self

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from protocol import (
    EntityId,
    FrozenStrictModel,
    NodeType,
    PlannerRunStatus,
    PlannerType,
    Sha256Hex,
    canonical_json,
)

from .planner import BasePlanner, PlannerInput, PlannerOutput

_ENTITY_ID = TypeAdapter(EntityId)
ErrorMessage = Annotated[str, Field(min_length=1, max_length=1_000)]


class PlannerFailureCode(StrEnum):
    UNAVAILABLE = "planner_unavailable"
    EXECUTION_FAILED = "planner_execution_failed"
    TIMED_OUT = "planner_timed_out"
    INVALID_OUTPUT = "planner_invalid_output"


class InvalidPlannerOutput(ValueError):
    """Raised when an adapter returns an untrusted or mismatched envelope."""


class PlannerRunMetadata(FrozenStrictModel):
    """Independent audit metadata for one primary or fallback attempt."""

    planner_run_id: EntityId
    planner_id: EntityId
    planner_type: PlannerType
    status: PlannerRunStatus
    context_bundle_sha256: Sha256Hex
    fallback_from_run_id: EntityId | None = None
    result_draft_sha256: Sha256Hex | None = None
    error_code: PlannerFailureCode | None = None
    error_message: ErrorMessage | None = None

    @model_validator(mode="after")
    def validate_status_evidence(self) -> Self:
        if self.status == PlannerRunStatus.SUCCEEDED:
            if self.result_draft_sha256 is None:
                raise ValueError("successful planner run requires result_draft_sha256")
            if self.error_code is not None or self.error_message is not None:
                raise ValueError("successful planner run cannot carry failure evidence")
        elif self.status in {PlannerRunStatus.FAILED, PlannerRunStatus.TIMED_OUT}:
            if self.error_code is None or self.error_message is None:
                raise ValueError("failed planner run requires error evidence")
            if self.result_draft_sha256 is not None:
                raise ValueError("failed planner run cannot carry a result hash")
        return self


class PlannerCoordinationResult(FrozenStrictModel):
    primary_run: PlannerRunMetadata
    fallback_run: PlannerRunMetadata | None = None
    final_output: PlannerOutput | None = None


class PlannerCoordinator:
    """Run an OpenCode planner and retain separate RuleBased fallback evidence."""

    def __init__(
        self,
        primary_planner: BasePlanner | None,
        fallback_planner: BasePlanner,
        *,
        primary_timeout_seconds: float = 300.0,
        fallback_timeout_seconds: float = 30.0,
        unavailable_primary_id: str = "opencode-planner",
    ) -> None:
        if primary_timeout_seconds <= 0 or fallback_timeout_seconds <= 0:
            raise ValueError("planner timeouts must be positive")
        if primary_planner is not None and primary_planner.planner_type != PlannerType.OPEN_CODE:
            raise ValueError("primary planner must use planner_type=open_code")
        if fallback_planner.planner_type != PlannerType.RULE_BASED:
            raise ValueError("fallback planner must use planner_type=rule_based")
        self._primary = primary_planner
        self._fallback = fallback_planner
        self._primary_timeout = primary_timeout_seconds
        self._fallback_timeout = fallback_timeout_seconds
        self._unavailable_primary_id = _ENTITY_ID.validate_python(unavailable_primary_id)

    async def plan_with_fallback(
        self,
        planner_input: PlannerInput,
        *,
        primary_run_id: EntityId,
        fallback_run_id: EntityId | None = None,
    ) -> PlannerCoordinationResult:
        if fallback_run_id is not None and fallback_run_id == primary_run_id:
            raise ValueError("primary and fallback planner_run IDs must be distinct")

        if self._primary is None:
            primary_run = PlannerRunMetadata(
                planner_run_id=primary_run_id,
                planner_id=self._unavailable_primary_id,
                planner_type=PlannerType.OPEN_CODE,
                status=PlannerRunStatus.FAILED,
                context_bundle_sha256=planner_input.context_bundle.bundle_hash,
                error_code=PlannerFailureCode.UNAVAILABLE,
                error_message="OpenCode planner unavailable",
            )
            primary_output = None
        else:
            primary_run, primary_output = await self._run_attempt(
                self._primary,
                planner_input,
                run_id=primary_run_id,
                timeout_seconds=self._primary_timeout,
                fallback_from_run_id=None,
            )

        if primary_output is not None:
            return PlannerCoordinationResult(
                primary_run=primary_run,
                final_output=primary_output,
            )

        if fallback_run_id is None:
            raise ValueError("fallback_run_id required when primary fails")

        fallback_run, fallback_output = await self._run_attempt(
            self._fallback,
            planner_input,
            run_id=fallback_run_id,
            timeout_seconds=self._fallback_timeout,
            fallback_from_run_id=primary_run_id,
        )
        return PlannerCoordinationResult(
            primary_run=primary_run,
            fallback_run=fallback_run,
            final_output=fallback_output,
        )

    async def _run_attempt(
        self,
        planner: BasePlanner,
        planner_input: PlannerInput,
        *,
        run_id: EntityId,
        timeout_seconds: float,
        fallback_from_run_id: EntityId | None,
    ) -> tuple[PlannerRunMetadata, PlannerOutput | None]:
        try:
            output = await asyncio.wait_for(
                planner.plan(planner_input),
                timeout=timeout_seconds,
            )
            _validate_output(output, planner, planner_input)
        except TimeoutError:
            return (
                PlannerRunMetadata(
                    planner_run_id=run_id,
                    planner_id=planner.planner_id,
                    planner_type=planner.planner_type,
                    status=PlannerRunStatus.TIMED_OUT,
                    context_bundle_sha256=planner_input.context_bundle.bundle_hash,
                    fallback_from_run_id=fallback_from_run_id,
                    error_code=PlannerFailureCode.TIMED_OUT,
                    error_message=f"planner exceeded {timeout_seconds:g} second timeout",
                ),
                None,
            )
        except (InvalidPlannerOutput, JSONDecodeError, ValidationError) as error:
            return (
                PlannerRunMetadata(
                    planner_run_id=run_id,
                    planner_id=planner.planner_id,
                    planner_type=planner.planner_type,
                    status=PlannerRunStatus.FAILED,
                    context_bundle_sha256=planner_input.context_bundle.bundle_hash,
                    fallback_from_run_id=fallback_from_run_id,
                    error_code=PlannerFailureCode.INVALID_OUTPUT,
                    error_message=_safe_error_message(error),
                ),
                None,
            )
        except Exception as error:
            return (
                PlannerRunMetadata(
                    planner_run_id=run_id,
                    planner_id=planner.planner_id,
                    planner_type=planner.planner_type,
                    status=PlannerRunStatus.FAILED,
                    context_bundle_sha256=planner_input.context_bundle.bundle_hash,
                    fallback_from_run_id=fallback_from_run_id,
                    error_code=PlannerFailureCode.EXECUTION_FAILED,
                    error_message=_safe_error_message(error),
                ),
                None,
            )

        result_hash = Sha256Hex(sha256(canonical_json(output.draft)).hexdigest())
        return (
            PlannerRunMetadata(
                planner_run_id=run_id,
                planner_id=planner.planner_id,
                planner_type=planner.planner_type,
                status=PlannerRunStatus.SUCCEEDED,
                context_bundle_sha256=planner_input.context_bundle.bundle_hash,
                fallback_from_run_id=fallback_from_run_id,
                result_draft_sha256=result_hash,
            ),
            output,
        )


def _validate_output(
    output: object,
    planner: BasePlanner,
    planner_input: PlannerInput,
) -> None:
    if not isinstance(output, PlannerOutput):
        raise InvalidPlannerOutput("planner did not return PlannerOutput")
    context = planner_input.context_bundle
    draft = output.draft
    if output.context_bundle_sha256 != context.bundle_hash:
        raise InvalidPlannerOutput("planner output references a different context bundle")
    if output.planner_type != planner.planner_type or draft.planner_type != planner.planner_type:
        raise InvalidPlannerOutput("planner output type does not match planner identity")
    if draft.planner_id != planner.planner_id:
        raise InvalidPlannerOutput("planner output ID does not match planner identity")
    if draft.session_id != context.session_id or draft.goal != context.goal:
        raise InvalidPlannerOutput("planner output does not match session goal")

    node_ids = [node.id for node in draft.nodes]
    if len(node_ids) != len(set(node_ids)):
        raise InvalidPlannerOutput("planner output contains duplicate node IDs")
    if sum(node.node_type == NodeType.INPUT for node in draft.nodes) != 1:
        raise InvalidPlannerOutput("planner output must contain exactly one input node")
    if sum(node.node_type == NodeType.OUTPUT for node in draft.nodes) != 1:
        raise InvalidPlannerOutput("planner output must contain exactly one output node")
    known_ids = set(node_ids)
    if any(
        edge.from_node not in known_ids or edge.to_node not in known_ids for edge in draft.edges
    ):
        raise InvalidPlannerOutput("planner output edge references an unknown node")


_SECRET_IN_ERROR = re.compile(
    r"(?i)(?:sk[-_]|ghp_)[A-Za-z0-9_-]{8,}|github_pat_[A-Za-z0-9_]{8,}"
    r"|xox[baprs]-[A-Za-z0-9-]{8,}|Bearer\s+[A-Za-z0-9._~+/-]{8,}=*"
    r"|(?:api[_-]?key|access[_-]?token|token|password|secret)\s*[:=]\s*\S+"
)


def _safe_error_message(error: Exception) -> str:
    message = " ".join(str(error).split()) or error.__class__.__name__
    message = _SECRET_IN_ERROR.sub("[REDACTED]", message)
    return message[:1_000]


class ParentWorkflowSnapshot(FrozenStrictModel):
    """Immutable identity loaded before creating a replan child."""

    workflow_id: EntityId
    session_id: EntityId
    semantic_version: int = Field(ge=1)
    author_graph_sha256: Sha256Hex


class WorkflowLineage(FrozenStrictModel):
    """Metadata for a newly inserted workflow row."""

    workflow_id: EntityId
    session_id: EntityId
    source_planner_run_id: EntityId
    parent_workflow_id: EntityId | None = None
    parent_semantic_version: int | None = Field(default=None, ge=1)
    parent_author_graph_sha256: Sha256Hex | None = None
    initial_semantic_version: Literal[1] = 1

    @model_validator(mode="after")
    def validate_parent_evidence(self) -> Self:
        values = (
            self.parent_workflow_id,
            self.parent_semantic_version,
            self.parent_author_graph_sha256,
        )
        if any(value is None for value in values) and any(value is not None for value in values):
            raise ValueError("parent lineage fields must be all present or all absent")
        return self


def build_workflow_lineage(
    *,
    workflow_id: str,
    session_id: str,
    source_planner_run_id: str,
    parent: ParentWorkflowSnapshot | None = None,
) -> WorkflowLineage:
    """Build child metadata without modifying the loaded parent snapshot."""

    resolved_workflow_id = _ENTITY_ID.validate_python(workflow_id)
    resolved_session_id = _ENTITY_ID.validate_python(session_id)
    resolved_run_id = _ENTITY_ID.validate_python(source_planner_run_id)
    if parent is None:
        return WorkflowLineage(
            workflow_id=resolved_workflow_id,
            session_id=resolved_session_id,
            source_planner_run_id=resolved_run_id,
        )
    if parent.session_id != resolved_session_id:
        raise ValueError("parent workflow must belong to the same session")
    if parent.workflow_id == resolved_workflow_id:
        raise ValueError("replan must create a new workflow instead of mutating its parent")
    return WorkflowLineage(
        workflow_id=resolved_workflow_id,
        session_id=resolved_session_id,
        source_planner_run_id=resolved_run_id,
        parent_workflow_id=parent.workflow_id,
        parent_semantic_version=parent.semantic_version,
        parent_author_graph_sha256=parent.author_graph_sha256,
    )
