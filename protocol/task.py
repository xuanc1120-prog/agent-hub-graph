"""Frozen v1 TaskPackage contract.

``TaskPackage`` is the sealed unit of work handed to an agent for a single
``node_run``. Its ``effective_*`` scope, initial ``effective_risk`` and
``requires_changeset_approval`` are copied from the ``CompiledGraph`` snapshot
of the owning ``workflow_run`` -- never from AuthorGraph candidate fields.
Runtime guards may tighten scope or raise risk, never widen compiled scope or
lower ``policy_risk_floor``. The single exception is an atomically-consumed
``CapabilityGrant`` bound to this task: its one exact existing resource is
carried separately in ``active_capability_grant_id`` / ``granted_existing_files``
and never rewrites the compiled ``effective_*`` fields.
"""

from __future__ import annotations

from pydantic import Field

from protocol.common import (
    ArtifactRef,
    CommandTemplate,
    EntityId,
    GitObjectId,
    InstructionText,
    RepoRelativePath,
    RiskLevel,
    Sha256Hex,
    ShortReasonText,
    StrictModel,
    TaskKind,
)


class TaskPackage(StrictModel):
    task_id: EntityId
    session_id: EntityId
    workflow_run_id: EntityId
    node_run_id: EntityId
    node_id: EntityId
    agent_id: EntityId
    task_kind: TaskKind
    instruction: InstructionText
    repo_path: RepoRelativePath
    base_commit: GitObjectId
    effective_allowed_files: list[RepoRelativePath] = Field(default_factory=list, max_length=100)
    effective_new_files: list[RepoRelativePath] = Field(default_factory=list, max_length=100)
    active_capability_grant_id: EntityId | None = None
    granted_existing_files: list[RepoRelativePath] = Field(default_factory=list, max_length=1)
    readonly_files: list[RepoRelativePath] = Field(default_factory=list, max_length=2_000)
    effective_allowed_commands: list[CommandTemplate] = Field(default_factory=list, max_length=20)
    workspace_ephemeral_paths: list[RepoRelativePath] = Field(default_factory=list, max_length=100)
    forbidden_actions: list[ShortReasonText] = Field(default_factory=list, max_length=100)
    acceptance_criteria: list[ShortReasonText] = Field(default_factory=list, max_length=100)
    effective_risk: RiskLevel = RiskLevel.L1
    requires_changeset_approval: bool = False
    runtime_policy_ref: ArtifactRef
    context_bundle_path: RepoRelativePath
    context_bundle_sha256: Sha256Hex
    timeout_seconds: int = Field(default=900, ge=1, le=3600)
