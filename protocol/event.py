"""Frozen v1 Artifact and Event contracts.

Every ``event_type`` maps (in the runtime ``EventRegistry``) to exactly one
``StrictModel`` payload class; the Repository validates against the registry
and then caps the canonical payload JSON at 64 KiB before persisting. Large
content is never inlined -- only an :class:`ArtifactRef` is stored. ``Artifact``
owners are mutually exclusive: a task-owned artifact and a planner-run-owned
artifact cannot both be set.
"""

from __future__ import annotations

from datetime import datetime
from typing import Generic, Self, TypeVar

from pydantic import Field, model_validator

from protocol.common import (
    ActorType,
    ArtifactType,
    EntityId,
    FrozenStrictModel,
    RepoRelativePath,
    Sha256Hex,
    StrictModel,
)


class Artifact(FrozenStrictModel):
    # Frozen: cross-field validator + Pydantic's assign-then-validate order means
    # a failed assignment could otherwise leave the record illegal. Artifacts are
    # immutable audit records anyway. See ADR 0001.
    artifact_id: EntityId
    session_id: EntityId
    task_id: EntityId | None = None
    planner_run_id: EntityId | None = None
    artifact_type: ArtifactType
    relative_path: RepoRelativePath
    sha256: Sha256Hex
    size_bytes: int = Field(ge=0)
    redacted: bool
    created_at: datetime

    @model_validator(mode="after")
    def _check_owner_exclusive(self) -> Self:
        if self.task_id is not None and self.planner_run_id is not None:
            raise ValueError("artifact task and planner owners are mutually exclusive")
        return self


PayloadT = TypeVar("PayloadT", bound=StrictModel)


class EventEnvelope(FrozenStrictModel, Generic[PayloadT]):
    # Frozen: see Artifact / ADR 0001. Events are immutable, append-only records.
    event_id: int = Field(ge=1)
    session_id: EntityId
    workflow_id: EntityId | None = None
    workflow_run_id: EntityId | None = None
    run_seq: int | None = Field(default=None, ge=1)
    event_type: EntityId
    actor_type: ActorType
    actor_id: EntityId | None = None
    payload: PayloadT
    created_at: datetime

    @model_validator(mode="after")
    def _check_run_fields(self) -> Self:
        if (self.workflow_run_id is None) != (self.run_seq is None):
            raise ValueError("workflow_run_id and run_seq must be set together")
        if self.workflow_run_id is not None and self.workflow_id is None:
            raise ValueError("a run event must also carry workflow_id")
        return self
