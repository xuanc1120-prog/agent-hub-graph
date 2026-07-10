"""Frozen v1 ConsoleChunk contract.

Console bodies are stored only as de-identified ArtifactStore chunks (<=64 KiB
each); SQLite keeps just the artifact reference, sequence and size. A chunk's
referenced artifact must be of type ``console`` and its declared size must match
the referenced artifact.
"""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import Field, model_validator

from protocol.common import (
    ArtifactRef,
    ArtifactType,
    ConsoleStreamKind,
    EntityId,
    StrictModel,
)


class ConsoleChunk(StrictModel):
    console_session_id: EntityId
    seq: int = Field(ge=1)
    stream: ConsoleStreamKind
    artifact_ref: ArtifactRef
    size_bytes: int = Field(gt=0, le=65_536)
    created_at: datetime

    @model_validator(mode="after")
    def _check_console_artifact(self) -> Self:
        if self.artifact_ref.artifact_type is not ArtifactType.CONSOLE:
            raise ValueError("console chunk artifact_ref must be of type console")
        if self.artifact_ref.size_bytes != self.size_bytes:
            raise ValueError("console chunk size_bytes must match artifact_ref.size_bytes")
        return self
