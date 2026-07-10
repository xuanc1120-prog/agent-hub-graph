"""Frozen v1 ContextPack contract.

A ``ContextPack`` carries only typed metadata and artifact references under a
prompt/token budget. It never embeds raw sensitive file content; selected
artifacts are fetched by the Executor through an isolated read-only
TaskContextBundle.
"""

from __future__ import annotations

from pydantic import Field

from protocol.common import (
    ArtifactRef,
    EntityId,
    NodeRunStatus,
    RepoRelativePath,
    ShortReasonText,
    StrictModel,
    SummaryText,
    TaskKind,
    TitleText,
)


class NodeSummary(StrictModel):
    node_run_id: EntityId
    status: NodeRunStatus
    summary: SummaryText = ""
    artifact_refs: list[ArtifactRef] = Field(default_factory=list, max_length=500)


class ContextPack(StrictModel):
    task_id: EntityId
    node_id: EntityId
    task_kind: TaskKind
    session_goal: str = Field(max_length=20_000)
    current_node_title: TitleText
    current_task: str = Field(max_length=20_000)
    upstream_summaries: list[NodeSummary] = Field(default_factory=list, max_length=100)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list, max_length=500)
    effective_allowed_files: list[RepoRelativePath] = Field(default_factory=list, max_length=100)
    effective_new_files: list[RepoRelativePath] = Field(default_factory=list, max_length=100)
    active_capability_grant_id: EntityId | None = None
    granted_existing_files: list[RepoRelativePath] = Field(default_factory=list, max_length=1)
    effective_allowed_commands: list[list[str]] = Field(default_factory=list, max_length=20)
    forbidden_paths: list[RepoRelativePath] = Field(default_factory=list, max_length=2_000)
    acceptance_criteria: list[ShortReasonText] = Field(default_factory=list, max_length=100)
    max_prompt_chars: int = Field(default=12_000, ge=1_000, le=24_000)
