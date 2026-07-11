"""Frozen v1 AgentResult and AgentOutputEnvelope contracts.

``AgentResult`` is the Master-side normalized outcome of a single agent run.
``AgentOutputEnvelope`` is the strict, size-bounded structure an agent's
de-identified output must parse into; unstructured long output is stored as an
artifact and referenced, never inlined.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from protocol.common import (
    ArtifactRef,
    EntityId,
    ShortReasonText,
    StrictModel,
    SummaryText,
)


class AgentResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PRIVILEGE_REQUESTED = "privilege_requested"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    BLOCKED_BY_GUARD = "blocked_by_guard"
    PARSE_FAILED = "parse_failed"


class AgentResult(StrictModel):
    task_id: EntityId
    node_run_id: EntityId
    agent_id: EntityId
    status: AgentResultStatus
    summary: SummaryText = ""
    raw_output_ref: ArtifactRef | None = None
    change_set_id: EntityId | None = None
    artifact_refs: list[ArtifactRef] = Field(default_factory=list, max_length=500)
    risks: list[ShortReasonText] = Field(default_factory=list, max_length=50)
    privilege_request_ids: list[EntityId] = Field(default_factory=list, max_length=1)
    error_code: str | None = Field(default=None, max_length=200)
    error_message: str | None = Field(default=None, max_length=8_000)


class NextSuggestion(StrictModel):
    suggested_agent: EntityId | None = None
    reason: ShortReasonText
