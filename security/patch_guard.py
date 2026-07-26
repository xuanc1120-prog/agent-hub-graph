"""Action-aware validation for immutable ChangeSet artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path, PurePosixPath

from security.path_policy import PathPolicy, PathPolicyViolation
from workspace.change_set import ChangeSetManifest, FileAction

_FORBIDDEN_GIT_FILES = frozenset({".gitattributes", ".gitmodules"})
_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,255}\b"),
)


class PatchGuardDecision(StrEnum):
    PASSED = "passed"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class PatchGuardReport:
    decision: PatchGuardDecision
    patch_sha256: str
    checked_paths: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.decision == PatchGuardDecision.PASSED


class PatchGuard:
    def __init__(
        self,
        repo_root: Path,
        *,
        max_patch_bytes: int = 20 * 1024 * 1024,
        max_changed_paths: int = 500,
    ) -> None:
        if max_patch_bytes < 1 or max_changed_paths < 1:
            raise ValueError("PatchGuard limits must be positive")
        self._policy = PathPolicy(repo_root, max_scope_files=100)
        self._max_patch_bytes = max_patch_bytes
        self._max_changed_paths = max_changed_paths

    def check(
        self,
        manifest: ChangeSetManifest,
        patch_bytes: bytes,
        *,
        allowed_existing_files: tuple[str, ...] | list[str],
        allowed_new_files: tuple[str, ...] | list[str],
        expected_patch_sha256: str | None = None,
    ) -> PatchGuardReport:
        patch_hash = sha256(patch_bytes).hexdigest()
        checked_paths = self._changed_paths(manifest)
        reasons: list[str] = []
        if expected_patch_sha256 is not None and patch_hash != expected_patch_sha256:
            reasons.append("patch_hash_mismatch")
        if len(patch_bytes) > self._max_patch_bytes:
            reasons.append("patch_size_limit_exceeded")
        if len(manifest.changes) > self._max_changed_paths:
            reasons.append("changed_path_limit_exceeded")
        if manifest.changes and not patch_bytes:
            reasons.append("nonempty_changeset_has_empty_patch")
        if manifest.ignored_files_touched:
            reasons.append("ignored_files_touched")

        try:
            scope = self._policy.validate_scope(
                allowed_existing_files,
                allowed_new_files,
            )
        except PathPolicyViolation as error:
            reason = str(error)
            decision = (
                PatchGuardDecision.QUARANTINED
                if "sensitive" in reason
                else PatchGuardDecision.REJECTED
            )
            return PatchGuardReport(
                decision=decision,
                patch_sha256=patch_hash,
                checked_paths=checked_paths,
                reasons=(f"invalid_effective_scope:{reason}",),
            )

        existing = {self._policy.comparison_key(path): path for path in scope.existing_files}
        new = {self._policy.comparison_key(path): path for path in scope.new_files}
        created_files = tuple(
            change.path
            for change in manifest.changes
            if change.action in {FileAction.CREATED, FileAction.RENAMED}
        )
        for directory in manifest.created_directories:
            try:
                self._policy.validate_cleanup_path(directory)
            except PathPolicyViolation as error:
                reasons.append(f"invalid_created_directory:{directory}:{error}")
                continue
            prefix = f"{directory}/"
            if not any(path.startswith(prefix) for path in created_files):
                reasons.append(f"empty_or_unowned_created_directory:{directory}")
        seen: set[str] = set()
        for change in manifest.changes:
            paths = (
                (change.old_path, change.path) if change.old_path is not None else (change.path,)
            )
            for path in paths:
                key = self._policy.comparison_key(path)
                if key in seen:
                    reasons.append(f"duplicate_changed_path:{path}")
                seen.add(key)
                if PurePosixPath(path).name.casefold() in _FORBIDDEN_GIT_FILES:
                    reasons.append(f"forbidden_git_control_file:{path}")

            try:
                self._validate_action(change.action, change.path, change.old_path, existing, new)
            except (PathPolicyViolation, ValueError) as error:
                reasons.append(str(error))

        if any(pattern.search(patch_bytes) for pattern in _SECRET_PATTERNS):
            return PatchGuardReport(
                decision=PatchGuardDecision.QUARANTINED,
                patch_sha256=patch_hash,
                checked_paths=checked_paths,
                reasons=tuple((*reasons, "probable_secret_material_in_patch")),
            )
        if any("sensitive" in reason for reason in reasons):
            decision = PatchGuardDecision.QUARANTINED
        elif reasons:
            decision = PatchGuardDecision.REJECTED
        else:
            decision = PatchGuardDecision.PASSED
        return PatchGuardReport(
            decision=decision,
            patch_sha256=patch_hash,
            checked_paths=checked_paths,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    def _validate_action(
        self,
        action: FileAction,
        path: str,
        old_path: str | None,
        existing: dict[str, str],
        new: dict[str, str],
    ) -> None:
        path_key = self._policy.comparison_key(path)
        if action in {FileAction.MODIFIED, FileAction.DELETED}:
            self._policy.validate_captured_path(path, must_exist=True)
            if path_key not in existing:
                raise ValueError(f"existing_scope_violation:{path}")
            return
        if action == FileAction.CREATED:
            self._policy.validate_captured_path(path, must_exist=False)
            if path_key not in new:
                raise ValueError(f"new_scope_violation:{path}")
            return
        if action == FileAction.RENAMED:
            if old_path is None:
                raise ValueError(f"rename_missing_source:{path}")
            old_key = self._policy.comparison_key(old_path)
            if old_key == path_key:
                raise ValueError(f"case_or_unicode_only_rename:{old_path}:{path}")
            self._policy.validate_captured_path(old_path, must_exist=True)
            self._policy.validate_captured_path(path, must_exist=False)
            if old_key not in existing:
                raise ValueError(f"rename_source_scope_violation:{old_path}")
            if path_key not in new:
                raise ValueError(f"rename_destination_scope_violation:{path}")
            return
        raise ValueError(f"unsupported_change_action:{action}")

    @staticmethod
    def _changed_paths(manifest: ChangeSetManifest) -> tuple[str, ...]:
        values = [
            path
            for change in manifest.changes
            for path in ((change.old_path, change.path) if change.old_path else (change.path,))
        ]
        values.extend(manifest.ignored_files_touched)
        return tuple(sorted(values))


__all__ = [
    "PatchGuard",
    "PatchGuardDecision",
    "PatchGuardReport",
]
