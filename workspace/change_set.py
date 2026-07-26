"""Internal immutable ChangeSet manifest used by workspace and guard layers."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from protocol.common import (
    FrozenStrictModel,
    GitObjectId,
    RepoRelativePath,
    Sha256Hex,
)


class FileAction(StrEnum):
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


class FileChange(FrozenStrictModel):
    action: FileAction
    path: RepoRelativePath
    old_path: RepoRelativePath | None = None
    before_sha256: Sha256Hex | None = None
    after_sha256: Sha256Hex | None = None
    before_size: int | None = Field(default=None, ge=0)
    after_size: int | None = Field(default=None, ge=0)
    binary: bool = False

    @model_validator(mode="after")
    def validate_action_shape(self) -> FileChange:
        if self.action == FileAction.CREATED:
            if self.old_path is not None or self.before_sha256 is not None:
                raise ValueError("created files cannot carry old_path or before_sha256")
            if self.after_sha256 is None or self.after_size is None:
                raise ValueError("created files require after hash and size")
        elif self.action == FileAction.DELETED:
            if self.old_path is not None or self.after_sha256 is not None:
                raise ValueError("deleted files cannot carry old_path or after_sha256")
            if self.before_sha256 is None or self.before_size is None:
                raise ValueError("deleted files require before hash and size")
        elif self.action == FileAction.MODIFIED:
            if self.old_path is not None:
                raise ValueError("modified files cannot carry old_path")
            if (
                self.before_sha256 is None
                or self.after_sha256 is None
                or self.before_size is None
                or self.after_size is None
            ):
                raise ValueError("modified files require before and after metadata")
        elif self.action == FileAction.RENAMED:
            if self.old_path is None or self.old_path == self.path:
                raise ValueError("renamed files require a distinct old_path")
            if (
                self.before_sha256 is None
                or self.after_sha256 is None
                or self.before_size is None
                or self.after_size is None
            ):
                raise ValueError("renamed files require before and after metadata")
        return self


class ChangeSetManifest(FrozenStrictModel):
    base_commit: GitObjectId
    pre_state_hash: Sha256Hex
    post_state_hash: Sha256Hex
    changes: tuple[FileChange, ...] = Field(default_factory=tuple, max_length=500)
    created_directories: tuple[RepoRelativePath, ...] = Field(
        default_factory=tuple,
        max_length=500,
    )
    ignored_files_touched: tuple[RepoRelativePath, ...] = Field(
        default_factory=tuple,
        max_length=500,
    )
    staged_evidence_sha256: Sha256Hex
    unstaged_evidence_sha256: Sha256Hex
    status_evidence_sha256: Sha256Hex

    @model_validator(mode="after")
    def reject_duplicate_paths(self) -> ChangeSetManifest:
        paths: list[str] = []
        for change in self.changes:
            paths.append(change.path)
            if change.old_path is not None:
                paths.append(change.old_path)
        if len(paths) != len(set(paths)):
            raise ValueError("ChangeSet paths must be unique")
        return self


__all__ = ["ChangeSetManifest", "FileAction", "FileChange"]
