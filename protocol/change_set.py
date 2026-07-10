"""Frozen v1 ChangeSet contract.

``ChangeSetStatus`` is an independent persistent state machine, not a display
label. It advances only along the whitelisted transitions defined in the
development plan; guard / test / policy / user rejection, stale, partial and
quarantine states are kept distinct and terminal states never regress.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from protocol.common import (
    ArtifactRef,
    EntityId,
    GitObjectId,
    RepoRelativePath,
    Sha256Hex,
    StrictModel,
)


class ChangeSetStatus(StrEnum):
    CAPTURED = "captured"
    GUARD_PASSED = "guard_passed"
    GUARD_REJECTED = "guard_rejected"
    TEST_PASSED = "test_passed"
    TEST_FAILED = "test_failed"
    POLICY_REJECTED = "policy_rejected"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    STALE = "stale"
    MERGED = "merged"
    ABANDONED_PARTIAL = "abandoned_partial"
    QUARANTINED = "quarantined"


class ChangeSet(StrictModel):
    change_set_id: EntityId
    session_id: EntityId
    workflow_run_id: EntityId
    node_run_id: EntityId
    task_id: EntityId
    base_commit: GitObjectId
    pre_state_hash: Sha256Hex
    post_state_hash: Sha256Hex
    patch_sha256: Sha256Hex
    status: ChangeSetStatus = ChangeSetStatus.CAPTURED
    canonical_patch_ref: ArtifactRef
    evidence_refs: list[ArtifactRef] = Field(default_factory=list, max_length=500)
    created_files: list[RepoRelativePath] = Field(default_factory=list, max_length=500)
    created_directories: list[RepoRelativePath] = Field(default_factory=list, max_length=500)
    modified_files: list[RepoRelativePath] = Field(default_factory=list, max_length=500)
    deleted_files: list[RepoRelativePath] = Field(default_factory=list, max_length=500)
    renamed_files: list[RepoRelativePath] = Field(default_factory=list, max_length=500)
    untracked_files: list[RepoRelativePath] = Field(default_factory=list, max_length=500)
    ignored_files_touched: list[RepoRelativePath] = Field(default_factory=list, max_length=500)
    preimage_refs: list[ArtifactRef] = Field(default_factory=list, max_length=500)
