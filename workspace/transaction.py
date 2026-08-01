"""Capture a complete task ChangeSet and restore the shared repo exactly."""

from __future__ import annotations

import json
import os
import stat
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from security.path_policy import PathPolicy, PathPolicyViolation
from security.secret_policy import SecretPolicyViolation, assert_secret_free_bytes
from workspace.change_set import ChangeSetManifest, FileAction, FileChange
from workspace.git_manager import (
    CanonicalBaselineFile,
    CanonicalCapturedFile,
    GitManager,
    GitManagerError,
    GitMetadataSeal,
    RepositoryState,
)
from workspace.secure_file import (
    SecureFileError,
    SecureWorkspaceRoot,
    assert_path_identity,
    open_verified_binary,
    set_verified_mode,
)


class WorkspaceTransactionError(RuntimeError):
    """Base class for workspace capture and restoration failures."""


class WorkspaceNotClean(WorkspaceTransactionError):
    """The repository did not match the expected clean base at begin time."""


class WorkspaceRestoreError(WorkspaceTransactionError):
    """Exact task-local restoration failed and the workspace must be orphaned."""


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    path: str
    sha256: str
    size_bytes: int
    mode: int
    binary: bool


@dataclass(frozen=True, slots=True)
class _SealedFile:
    content: bytes
    sha256: str
    size_bytes: int
    mode: int


@dataclass(frozen=True, slots=True)
class FilePreimage:
    path: str
    sha256: str
    size_bytes: int
    mode: int
    content: bytes
    baseline_ignored: bool


@dataclass(frozen=True, slots=True)
class CapturedWorkspaceChangeSet:
    manifest: ChangeSetManifest
    patch_bytes: bytes
    status_evidence: bytes
    staged_evidence: bytes
    unstaged_evidence: bytes
    preimages: tuple[FilePreimage, ...]


@dataclass(frozen=True, slots=True)
class _Inventory:
    files: dict[str, FileSnapshot]
    directories: tuple[str, ...]
    state_hash: str
    total_bytes: int
    sealed_files: dict[str, _SealedFile]


class WorkspaceTransaction:
    """Single-use capture/restore state machine for one workspace lease."""

    def __init__(
        self,
        git: GitManager,
        repo: Path,
        *,
        base_commit: str,
        temp_directory: Path,
        expected_branch: str | None = None,
        max_changed_paths: int = 500,
        max_patch_bytes: int = 20 * 1024 * 1024,
        max_created_bytes: int = 100 * 1024 * 1024,
        max_inventory_paths: int = 50_000,
        max_inventory_bytes: int = 2 * 1024 * 1024 * 1024,
        max_ignored_preimage_bytes: int = 100 * 1024 * 1024,
        seal_git_objects: bool = False,
    ) -> None:
        if any(
            limit < 1
            for limit in (
                max_changed_paths,
                max_patch_bytes,
                max_created_bytes,
                max_inventory_paths,
                max_inventory_bytes,
                max_ignored_preimage_bytes,
            )
        ):
            raise ValueError("WorkspaceTransaction limits must be positive")
        self._git = git
        self._repo = repo.expanduser().resolve(strict=True)
        self._base_commit = base_commit
        self._temp_directory = temp_directory.expanduser().resolve(strict=False)
        self._expected_branch = expected_branch
        self._max_changed_paths = max_changed_paths
        self._max_patch_bytes = max_patch_bytes
        self._max_created_bytes = max_created_bytes
        self._max_inventory_paths = max_inventory_paths
        self._max_inventory_bytes = max_inventory_bytes
        self._max_ignored_preimage_bytes = max_ignored_preimage_bytes
        self._max_sealed_bytes = max(
            max_patch_bytes,
            max_created_bytes,
            max_ignored_preimage_bytes,
        )
        self._seal_git_objects = seal_git_objects
        self._path_policy = PathPolicy(self._repo, max_scope_files=max_changed_paths)
        self._phase = "new"
        self._baseline_state: RepositoryState | None = None
        self._baseline_inventory: _Inventory | None = None
        self._baseline_tracked: frozenset[str] = frozenset()
        self._baseline_ignored: frozenset[str] = frozenset()
        self._ignored_preimages: dict[str, FilePreimage] = {}
        self._baseline_index_sha256: str | None = None
        self._git_metadata_seal: GitMetadataSeal | None = None
        self._secure_root: SecureWorkspaceRoot | None = None

    def begin(self) -> None:
        if self._phase != "new":
            raise WorkspaceTransactionError("workspace transaction can only begin once")
        self._secure_root = SecureWorkspaceRoot(self._repo)
        try:
            self._begin_pinned()
        except BaseException:
            self._close_secure_root()
            raise

    def _begin_pinned(self) -> None:
        secure_root = self._require_secure_root()
        secure_root.assert_root_identity()
        metadata_seal = self._git.capture_metadata_seal(
            self._repo,
            include_objects=self._seal_git_objects,
        )
        state = self._git.state(self._repo)
        secure_root.assert_root_identity()
        if state.commit != self._base_commit:
            raise WorkspaceNotClean("workspace HEAD does not match the task base commit")
        if self._expected_branch is not None and state.branch != self._expected_branch:
            raise WorkspaceNotClean(
                "workspace branch does not match the session integration branch"
            )
        if state.dirty:
            raise WorkspaceNotClean("workspace must be clean before task execution")
        ignored = frozenset(state.ignored_files)
        if len(ignored) > self._max_changed_paths:
            raise WorkspaceNotClean("ignored baseline exceeds the replayable workspace path limit")
        self._validate_ignored_baseline(ignored)
        inventory = self._scan_inventory(seal_paths=ignored)
        tracked = frozenset(self._git.tracked_paths(self._repo))
        preimages = self._capture_ignored_preimages(ignored, inventory)
        index_sha256 = self._git.index_sha256(self._repo)
        secure_root.assert_root_identity()
        self._baseline_state = state
        self._baseline_inventory = inventory
        self._baseline_tracked = tracked
        self._baseline_ignored = ignored
        self._ignored_preimages = preimages
        self._baseline_index_sha256 = index_sha256
        self._git_metadata_seal = metadata_seal
        self._phase = "active"

    @property
    def baseline_state_hash(self) -> str:
        return self._require_baseline_inventory().state_hash

    def capture_and_restore(self) -> CapturedWorkspaceChangeSet:
        if self._phase != "active":
            raise WorkspaceTransactionError("workspace transaction is not active")
        try:
            return self._capture_and_restore_pinned()
        finally:
            self._close_secure_root()

    def _capture_and_restore_pinned(self) -> CapturedWorkspaceChangeSet:
        if self._phase != "active":
            raise WorkspaceTransactionError("workspace transaction is not active")
        baseline_state = self._require_baseline_state()
        baseline = self._require_baseline_inventory()
        metadata_seal = self._require_git_metadata_seal()
        self._require_secure_root().assert_root_identity()
        try:
            self._git.assert_metadata_seal(
                self._repo,
                metadata_seal,
                include_objects=self._seal_git_objects,
            )
        except GitManagerError as error:
            self._phase = "orphaned"
            raise WorkspaceRestoreError(
                "session Git control metadata changed during task execution"
            ) from error
        current_state = self._git.state(self._repo)
        if (
            current_state.commit != baseline_state.commit
            or current_state.branch != baseline_state.branch
        ):
            self._phase = "orphaned"
            raise WorkspaceRestoreError("Agent changed workspace HEAD or branch")

        evidence = self._git.capture_evidence(self._repo)
        restore_paths = set(evidence.changed_paths)
        created_directories: tuple[str, ...] = ()
        ignored_candidates = tuple(
            sorted(self._baseline_ignored | frozenset(current_state.ignored_files))
        )
        ignored_touched = ignored_candidates
        result: CapturedWorkspaceChangeSet | None = None
        capture_error: BaseException | None = None
        try:
            created_directories = tuple(
                sorted(
                    set(self._scan_directories()) - set(baseline.directories),
                    key=lambda path: (path.count("/"), path),
                )
            )
            after = self._scan_inventory()
            restore_paths.update(self._inventory_changed_paths(baseline, after))
            after = self._seal_inventory_paths(after, restore_paths)
            ignored_touched = self._ignored_changes(after, current_state)
            self._validate_ignored_capture(ignored_touched, after)
            self._validate_changed_paths(restore_paths, after)
            self._assert_sealed_paths_current(restore_paths, after)
            excluded_ignored = (
                self._baseline_ignored | frozenset(current_state.ignored_files)
            ) - frozenset(ignored_touched)
            replay_baseline = self._project_inventory(baseline, excluded_ignored)
            replay_after = self._project_inventory(after, excluded_ignored)
            if len(restore_paths) > self._max_changed_paths:
                raise WorkspaceTransactionError(
                    f"ChangeSet exceeds {self._max_changed_paths} paths"
                )
            created_bytes = sum(
                snapshot.size_bytes
                for path, snapshot in after.files.items()
                if path not in baseline.files
            )
            if created_bytes > self._max_created_bytes:
                raise WorkspaceTransactionError(
                    f"task-created bytes exceed {self._max_created_bytes}"
                )
            canonical = self._git.build_canonical_patch(
                self._repo,
                base_commit=self._base_commit,
                changed_paths=tuple(restore_paths),
                temp_directory=self._temp_directory,
                baseline_files=tuple(
                    CanonicalBaselineFile(
                        path=preimage.path,
                        content=preimage.content,
                        mode=preimage.mode,
                    )
                    for path in ignored_touched
                    if (preimage := self._ignored_preimages.get(path)) is not None
                ),
                captured_files=tuple(
                    CanonicalCapturedFile(
                        path=path,
                        content=after.sealed_files[path].content,
                        mode=after.sealed_files[path].mode,
                    )
                    for path in sorted(restore_paths)
                    if path in after.files
                ),
            )
            if len(canonical.patch_bytes) > self._max_patch_bytes:
                raise WorkspaceTransactionError(
                    f"canonical patch exceeds {self._max_patch_bytes} bytes"
                )
            changes = self._build_file_changes(
                canonical.name_status_bytes,
                baseline,
                after,
            )
            manifest = ChangeSetManifest(
                base_commit=self._base_commit,
                pre_state_hash=replay_baseline.state_hash,
                post_state_hash=replay_after.state_hash,
                changes=changes,
                created_directories=created_directories,
                ignored_files_touched=ignored_touched,
                staged_evidence_sha256=sha256(evidence.staged_diff_bytes).hexdigest(),
                unstaged_evidence_sha256=sha256(evidence.unstaged_diff_bytes).hexdigest(),
                status_evidence_sha256=sha256(evidence.status_bytes).hexdigest(),
            )
            result = CapturedWorkspaceChangeSet(
                manifest=manifest,
                patch_bytes=canonical.patch_bytes,
                status_evidence=evidence.status_bytes,
                staged_evidence=evidence.staged_diff_bytes,
                unstaged_evidence=evidence.unstaged_diff_bytes,
                preimages=self._build_preimages(changes, ignored_touched),
            )
        except BaseException as error:
            capture_error = error
        try:
            self._restore(
                restore_paths,
                created_directories=created_directories,
                ignored_touched=ignored_touched,
            )
            self._assert_restored()
        except BaseException as restore_error:
            self._phase = "orphaned"
            if capture_error is not None:
                restore_error.add_note(f"capture also failed: {capture_error!r}")
            raise WorkspaceRestoreError(
                "task-local workspace restoration failed"
            ) from restore_error
        if capture_error is not None:
            self._phase = "closed"
            raise capture_error
        assert result is not None
        try:
            self._verify_canonical_patch(result, replay_after)
        except WorkspaceRestoreError:
            self._phase = "orphaned"
            raise
        except BaseException:
            self._phase = "closed"
            raise
        self._phase = "closed"
        return result

    def _verify_canonical_patch(
        self,
        result: CapturedWorkspaceChangeSet,
        post_inventory: _Inventory,
    ) -> None:
        if not result.patch_bytes:
            if result.manifest.pre_state_hash != result.manifest.post_state_hash:
                raise WorkspaceTransactionError(
                    "empty canonical patch does not reproduce the captured post-state"
                )
            return
        replay_root = self._git.create_private_temporary_directory(prefix="ah-replay-")
        try:
            replay_common = Path(os.path.commonpath((self._repo, replay_root)))
        except ValueError:
            replay_common = None
        if replay_common == self._repo:
            self._git.remove_private_temporary_directory(replay_root)
            raise WorkspaceTransactionError("replay root must be outside the session repository")
        replay_id = replay_root.name.removeprefix("ah-replay-")
        replay_repo = replay_root / "workspace" / "repo"
        try:
            source = self._git.inspect_source_repository(
                self._repo,
                base_ref=self._base_commit,
            )
            self._git.create_session_repository(
                source=source,
                destination=replay_repo,
                session_id=f"replay-{replay_id[:24]}",
            )
            materialize_workspace_preimages(
                replay_repo,
                tuple(
                    preimage
                    for path in result.manifest.ignored_files_touched
                    if (preimage := self._ignored_preimages.get(path)) is not None
                ),
                max_paths=self._max_changed_paths,
            )
            replay_baseline = self._scan_inventory(repo=replay_repo)
            if replay_baseline.state_hash != result.manifest.pre_state_hash:
                raise WorkspaceTransactionError(
                    "replay workspace does not match the captured pre-state"
                )
            self._git.apply_check(replay_repo, result.patch_bytes)
            self._git.apply_patch(replay_repo, result.patch_bytes)
            applied = self._scan_inventory(repo=replay_repo)
            if applied.state_hash != result.manifest.post_state_hash:
                raise WorkspaceTransactionError(
                    "canonical patch does not reproduce the captured post-state"
                )
            if applied.files != post_inventory.files:
                raise WorkspaceTransactionError(
                    "canonical replay inventory differs from captured inventory"
                )
        finally:
            self._git.remove_private_temporary_directory(replay_root)

    def _restore(
        self,
        changed_paths: set[str],
        *,
        created_directories: tuple[str, ...],
        ignored_touched: tuple[str, ...],
    ) -> None:
        tracked_existing = tuple(
            sorted(path for path in changed_paths if path in self._baseline_tracked)
        )
        ignored_existing = set(self._baseline_ignored)
        untracked_new = tuple(
            sorted(
                path
                for path in changed_paths
                if path not in self._baseline_tracked and path not in ignored_existing
            )
        )
        self._git.restore_existing_paths(
            self._repo,
            base_commit=self._base_commit,
            paths=tracked_existing,
        )
        baseline = self._require_baseline_inventory()
        for path in tracked_existing:
            snapshot = baseline.files.get(path)
            if snapshot is None:
                continue
            try:
                set_verified_mode(self._require_secure_root(), path, snapshot.mode)
            except SecureFileError as error:
                raise WorkspaceRestoreError(
                    f"tracked file mode restoration failed: {path}"
                ) from error
        self._git.unstage_new_paths(self._repo, untracked_new)
        for path in untracked_new:
            self._remove_exact_new_file(path)

        ignored_to_restore = set(ignored_touched)
        if not ignored_to_restore:
            current_ignored = set(self._git.state(self._repo).ignored_files)
            ignored_to_restore.update(current_ignored ^ self._baseline_ignored)
        for path in sorted(ignored_to_restore):
            preimage = self._ignored_preimages.get(path)
            if preimage is None:
                self._remove_exact_new_file(path)
            else:
                self._restore_preimage(preimage)

        for directory in sorted(
            created_directories,
            key=lambda path: (path.count("/"), path),
            reverse=True,
        ):
            validated = self._path_policy.validate_cleanup_path(directory)
            try:
                self._require_secure_root().remove_directory(
                    validated.relative_path,
                    missing_ok=True,
                )
            except SecureFileError as error:
                raise WorkspaceRestoreError(
                    f"created directory could not be safely removed: {directory}"
                ) from error

    def _assert_restored(self) -> None:
        baseline_state = self._require_baseline_state()
        baseline_inventory = self._require_baseline_inventory()
        self._git.assert_metadata_seal(
            self._repo,
            self._require_git_metadata_seal(),
            include_objects=self._seal_git_objects,
        )
        current = self._git.state(self._repo)
        if current != baseline_state:
            raise WorkspaceRestoreError("workspace Git state differs from its baseline")
        if self._git.index_sha256(self._repo) != self._baseline_index_sha256:
            raise WorkspaceRestoreError("real Git index differs from its baseline")
        if self._scan_inventory(seal_paths=self._baseline_ignored) != baseline_inventory:
            raise WorkspaceRestoreError("workspace file inventory differs from its baseline")

    def _scan_directories(self, *, repo: Path | None = None) -> tuple[str, ...]:
        root = self._repo if repo is None else repo.expanduser().resolve(strict=True)
        directories: list[str] = []
        for current_root, directory_names, _file_names in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            current = Path(current_root)
            safe_directories: list[str] = []
            for name in sorted(directory_names):
                if current == root and name.casefold() == ".git":
                    continue
                child = current / name
                self._assert_plain_entry(child, expect_directory=True)
                safe_directories.append(name)
                directories.append(self._relative_path(child, root=root))
                if len(directories) > self._max_inventory_paths:
                    raise WorkspaceTransactionError(
                        "workspace directory inventory path limit exceeded"
                    )
            directory_names[:] = safe_directories
        return tuple(sorted(directories))

    def _scan_inventory(
        self,
        *,
        repo: Path | None = None,
        seal_paths: frozenset[str] = frozenset(),
    ) -> _Inventory:
        root = self._repo if repo is None else repo.expanduser().resolve(strict=True)
        snapshot_root: Path | SecureWorkspaceRoot = (
            self._require_secure_root() if repo is None else root
        )
        files: dict[str, FileSnapshot] = {}
        sealed_files: dict[str, _SealedFile] = {}
        directories: list[str] = []
        total_bytes = 0
        sealed_bytes = 0
        for current_root, directory_names, file_names in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            current = Path(current_root)
            safe_directories: list[str] = []
            for name in sorted(directory_names):
                if current == root and name.casefold() == ".git":
                    continue
                child = current / name
                self._assert_plain_entry(child, expect_directory=True)
                safe_directories.append(name)
                directories.append(self._relative_path(child, root=root))
            directory_names[:] = safe_directories
            for name in sorted(file_names):
                child = current / name
                self._assert_plain_entry(child, expect_directory=False)
                relative = self._relative_path(child, root=root)
                snapshot, content = self._snapshot_file(
                    snapshot_root,
                    relative,
                    capture_content=relative in seal_paths,
                )
                files[relative] = snapshot
                if content is not None:
                    sealed_files[relative] = content
                    sealed_bytes += content.size_bytes
                    if sealed_bytes > self._max_sealed_bytes:
                        raise WorkspaceTransactionError(
                            "workspace sealed content byte limit exceeded"
                        )
                total_bytes += snapshot.size_bytes
                if len(files) > self._max_inventory_paths:
                    raise WorkspaceTransactionError("workspace inventory path limit exceeded")
                if total_bytes > self._max_inventory_bytes:
                    raise WorkspaceTransactionError("workspace inventory byte limit exceeded")
        state_hash = self._inventory_hash(files)
        return _Inventory(
            files=files,
            directories=tuple(sorted(directories)),
            state_hash=state_hash,
            total_bytes=total_bytes,
            sealed_files=sealed_files,
        )

    def _seal_inventory_paths(
        self,
        inventory: _Inventory,
        paths: set[str],
    ) -> _Inventory:
        sealed_files = dict(inventory.sealed_files)
        sealed_bytes = sum(item.size_bytes for item in sealed_files.values())
        for path in sorted(paths):
            expected = inventory.files.get(path)
            if expected is None or path in sealed_files:
                continue
            current, sealed = self._snapshot_file(
                self._require_secure_root(),
                path,
                capture_content=True,
            )
            if sealed is None or current != expected:
                raise WorkspaceTransactionError(
                    f"workspace path changed while content was sealed: {path}"
                )
            sealed_files[path] = sealed
            sealed_bytes += sealed.size_bytes
            if sealed_bytes > self._max_sealed_bytes:
                raise WorkspaceTransactionError("workspace sealed content byte limit exceeded")
        return _Inventory(
            files=inventory.files,
            directories=inventory.directories,
            state_hash=inventory.state_hash,
            total_bytes=inventory.total_bytes,
            sealed_files=sealed_files,
        )

    def _validate_ignored_baseline(self, ignored: frozenset[str]) -> None:
        for path in sorted(ignored):
            try:
                self._path_policy.validate_captured_path(path, must_exist=True)
            except PathPolicyViolation as error:
                raise WorkspaceNotClean(
                    "ignored baseline contains a forbidden or unsafe path"
                ) from error

    def _capture_ignored_preimages(
        self,
        ignored: frozenset[str],
        inventory: _Inventory,
    ) -> dict[str, FilePreimage]:
        total = 0
        preimages: dict[str, FilePreimage] = {}
        for path in sorted(ignored):
            snapshot = inventory.files.get(path)
            if snapshot is None:
                continue
            content = inventory.sealed_files.get(path)
            if content is None:
                raise WorkspaceNotClean("ignored baseline content was not sealed")
            self._assert_sealed_content(path, snapshot, content)
            try:
                assert_secret_free_bytes(
                    content.content,
                    label=f"ignored workspace file {path!r}",
                )
            except SecretPolicyViolation as error:
                raise WorkspaceNotClean(
                    "ignored baseline contains content that cannot enter artifacts"
                ) from error
            total += content.size_bytes
            if total > self._max_ignored_preimage_bytes:
                raise WorkspaceNotClean("ignored preimages exceed the workspace baseline limit")
            preimages[path] = FilePreimage(
                path=path,
                sha256=snapshot.sha256,
                size_bytes=snapshot.size_bytes,
                mode=snapshot.mode,
                content=content.content,
                baseline_ignored=True,
            )
        return preimages

    def _validate_ignored_capture(
        self,
        ignored_touched: tuple[str, ...],
        after: _Inventory,
    ) -> None:
        for path in ignored_touched:
            try:
                self._path_policy.validate_captured_path(
                    path,
                    must_exist=path in after.files,
                )
                baseline = self._ignored_preimages.get(path)
                if baseline is not None:
                    assert_secret_free_bytes(
                        baseline.content,
                        label=f"ignored preimage {path!r}",
                    )
                snapshot = after.files.get(path)
                if snapshot is not None:
                    content = after.sealed_files.get(path)
                    if content is None:
                        raise SecretPolicyViolation("ignored captured content was not sealed")
                    self._assert_sealed_content(path, snapshot, content)
                    assert_secret_free_bytes(
                        content.content,
                        label=f"ignored captured file {path!r}",
                    )
            except (PathPolicyViolation, SecretPolicyViolation) as error:
                raise WorkspaceTransactionError(
                    "ignored change contains a forbidden path or credential-like content"
                ) from error

    @staticmethod
    def _assert_sealed_content(
        path: str,
        snapshot: FileSnapshot,
        sealed: _SealedFile,
    ) -> None:
        if (
            snapshot.path != path
            or snapshot.size_bytes != sealed.size_bytes
            or snapshot.sha256 != sealed.sha256
            or snapshot.mode != sealed.mode
            or sealed.size_bytes != len(sealed.content)
            or sealed.sha256 != sha256(sealed.content).hexdigest()
        ):
            raise WorkspaceTransactionError(
                f"sealed workspace content does not match inventory: {path}"
            )

    def _assert_sealed_paths_current(
        self,
        paths: set[str],
        inventory: _Inventory,
    ) -> None:
        for path in sorted(paths):
            expected = inventory.files.get(path)
            validated = self._path_policy.validate_cleanup_path(path)
            if expected is None:
                if validated.exists:
                    raise WorkspaceTransactionError(
                        f"workspace path appeared after inventory: {path}"
                    )
                continue
            if not validated.exists:
                raise WorkspaceTransactionError(
                    f"workspace path disappeared after inventory: {path}"
                )
            current, sealed = self._snapshot_file(
                self._require_secure_root(),
                path,
                capture_content=True,
            )
            expected_sealed = inventory.sealed_files.get(path)
            if (
                sealed is None
                or expected_sealed is None
                or current != expected
                or sealed != expected_sealed
            ):
                raise WorkspaceTransactionError(f"workspace path changed after inventory: {path}")

    @staticmethod
    def _inventory_changed_paths(
        before: _Inventory,
        after: _Inventory,
    ) -> set[str]:
        return {
            path
            for path in before.files.keys() | after.files.keys()
            if before.files.get(path) != after.files.get(path)
        }

    def _ignored_changes(
        self,
        after: _Inventory,
        current_state: RepositoryState,
    ) -> tuple[str, ...]:
        candidates = self._baseline_ignored | frozenset(current_state.ignored_files)
        baseline = self._require_baseline_inventory()
        return tuple(
            sorted(path for path in candidates if baseline.files.get(path) != after.files.get(path))
        )

    def _validate_changed_paths(
        self,
        paths: set[str],
        after: _Inventory,
    ) -> None:
        for path in paths:
            validated = self._path_policy.validate_cleanup_path(path)
            if path in after.files and not validated.exists:
                raise WorkspaceTransactionError(f"captured path disappeared: {path}")

    def _build_file_changes(
        self,
        name_status: bytes,
        before: _Inventory,
        after: _Inventory,
    ) -> tuple[FileChange, ...]:
        records = self._nul_records(name_status)
        changes: list[FileChange] = []
        index = 0
        while index < len(records):
            status_code = records[index]
            index += 1
            action_code = status_code[:1]
            if action_code == b"R":
                if index + 1 >= len(records):
                    raise WorkspaceTransactionError("truncated rename status")
                old_path = self._decode_path(records[index])
                new_path = self._decode_path(records[index + 1])
                index += 2
                old = self._require_snapshot(before, old_path, "rename source")
                new = self._require_snapshot(after, new_path, "rename destination")
                changes.append(
                    FileChange(
                        action=FileAction.RENAMED,
                        old_path=old_path,
                        path=new_path,
                        before_sha256=old.sha256,
                        after_sha256=new.sha256,
                        before_size=old.size_bytes,
                        after_size=new.size_bytes,
                        binary=old.binary or new.binary,
                    )
                )
                continue
            if index >= len(records):
                raise WorkspaceTransactionError("truncated name-status record")
            path = self._decode_path(records[index])
            index += 1
            old = before.files.get(path)
            new = after.files.get(path)
            if action_code == b"A" and old is not None:
                action_code = b"M"
            if action_code == b"A":
                new = self._require_snapshot(after, path, "created file")
                changes.append(
                    FileChange(
                        action=FileAction.CREATED,
                        path=path,
                        after_sha256=new.sha256,
                        after_size=new.size_bytes,
                        binary=new.binary,
                    )
                )
            elif action_code in {b"M", b"T"}:
                old = self._require_snapshot(before, path, "modified preimage")
                new = self._require_snapshot(after, path, "modified file")
                changes.append(
                    FileChange(
                        action=FileAction.MODIFIED,
                        path=path,
                        before_sha256=old.sha256,
                        after_sha256=new.sha256,
                        before_size=old.size_bytes,
                        after_size=new.size_bytes,
                        binary=old.binary or new.binary,
                    )
                )
            elif action_code == b"D":
                old = self._require_snapshot(before, path, "deleted preimage")
                changes.append(
                    FileChange(
                        action=FileAction.DELETED,
                        path=path,
                        before_sha256=old.sha256,
                        before_size=old.size_bytes,
                        binary=old.binary,
                    )
                )
            else:
                raise WorkspaceTransactionError(
                    f"unsupported Git name-status action: {status_code!r}"
                )
        return tuple(changes)

    def _build_preimages(
        self,
        changes: tuple[FileChange, ...],
        ignored_touched: tuple[str, ...],
    ) -> tuple[FilePreimage, ...]:
        preimages = {
            path: self._ignored_preimages[path]
            for path in ignored_touched
            if path in self._ignored_preimages
        }
        for change in changes:
            if change.action == FileAction.CREATED:
                continue
            source_path = change.old_path or change.path
            if source_path in preimages:
                continue
            content = self._git.read_blob(
                self._repo,
                commit=self._base_commit,
                path=source_path,
            )
            expected = change.before_sha256
            digest = sha256(content).hexdigest()
            if expected is None or digest != expected:
                raise WorkspaceTransactionError(f"tracked preimage hash mismatch: {source_path}")
            snapshot = self._require_baseline_inventory().files[source_path]
            preimages[source_path] = FilePreimage(
                path=source_path,
                sha256=digest,
                size_bytes=len(content),
                mode=snapshot.mode,
                content=content,
                baseline_ignored=False,
            )
        return tuple(preimages[path] for path in sorted(preimages))

    def _remove_exact_new_file(self, path: str) -> None:
        validated = self._path_policy.validate_cleanup_path(path)
        try:
            self._require_secure_root().unlink_regular(
                validated.relative_path,
                missing_ok=True,
            )
        except SecureFileError as error:
            raise WorkspaceRestoreError(f"new path could not be safely removed: {path}") from error

    def _restore_preimage(self, preimage: FilePreimage) -> None:
        materialize_workspace_preimages(
            self._repo,
            (preimage,),
            max_paths=self._max_changed_paths,
            secure_root=self._require_secure_root(),
            path_policy=self._path_policy,
        )

    @staticmethod
    def _snapshot_file(
        root: Path | SecureWorkspaceRoot,
        relative: str,
        *,
        capture_content: bool,
    ) -> tuple[FileSnapshot, _SealedFile | None]:
        digest = sha256()
        size = 0
        binary = False
        captured = bytearray() if capture_content else None
        try:
            with open_verified_binary(root, relative) as stream:
                before = os.fstat(stream.fileno())
                while chunk := stream.read(1024 * 1024):
                    if size == 0 and b"\0" in chunk[:8192]:
                        binary = True
                    digest.update(chunk)
                    size += len(chunk)
                    if captured is not None:
                        captured.extend(chunk)
                after = os.fstat(stream.fileno())
        except SecureFileError as error:
            raise WorkspaceTransactionError(
                f"workspace file could not be safely opened: {relative}"
            ) from error
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise WorkspaceTransactionError(
                f"workspace file changed while inventory was read: {relative}"
            )
        try:
            assert_path_identity(root, relative, after)
        except SecureFileError as error:
            raise WorkspaceTransactionError(
                f"workspace file was replaced while inventory was read: {relative}"
            ) from error
        snapshot = FileSnapshot(
            path=relative,
            sha256=digest.hexdigest(),
            size_bytes=size,
            mode=stat.S_IMODE(after.st_mode),
            binary=binary,
        )
        content = (
            _SealedFile(
                content=bytes(captured),
                sha256=snapshot.sha256,
                size_bytes=snapshot.size_bytes,
                mode=snapshot.mode,
            )
            if captured is not None
            else None
        )
        return snapshot, content

    @staticmethod
    def _inventory_hash(files: dict[str, FileSnapshot]) -> str:
        payload = [
            [path, snapshot.sha256, snapshot.size_bytes, snapshot.mode]
            for path, snapshot in sorted(files.items())
        ]
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @classmethod
    def _project_inventory(
        cls,
        inventory: _Inventory,
        excluded_paths: frozenset[str],
    ) -> _Inventory:
        files = {
            path: snapshot
            for path, snapshot in inventory.files.items()
            if path not in excluded_paths
        }
        directories: set[str] = set()
        for path in files:
            parent = Path(path).parent
            while parent != Path("."):
                directories.add(parent.as_posix())
                parent = parent.parent
        return _Inventory(
            files=files,
            directories=tuple(sorted(directories)),
            state_hash=cls._inventory_hash(files),
            total_bytes=sum(snapshot.size_bytes for snapshot in files.values()),
            sealed_files={
                path: content for path, content in inventory.sealed_files.items() if path in files
            },
        )

    def _relative_path(self, path: Path, *, root: Path | None = None) -> str:
        relative = path.relative_to(root or self._repo).as_posix()
        if unicodedata.normalize("NFC", relative) != relative:
            raise WorkspaceTransactionError("workspace path is not Unicode NFC normalized")
        try:
            relative.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise WorkspaceTransactionError("workspace path is not stable UTF-8") from error
        if any(ord(character) < 32 or ord(character) == 127 for character in relative):
            raise WorkspaceTransactionError("workspace path contains control characters")
        return relative

    @staticmethod
    def _assert_plain_entry(path: Path, *, expect_directory: bool) -> None:
        metadata = path.lstat()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0) & reparse_flag
        ):
            raise WorkspaceTransactionError("workspace contains a symlink or reparse point")
        if expect_directory:
            if not stat.S_ISDIR(metadata.st_mode):
                raise WorkspaceTransactionError("workspace directory inventory is unstable")
        elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
            raise WorkspaceTransactionError(
                "workspace files must be regular and have one hard link"
            )

    @staticmethod
    def _nul_records(value: bytes) -> tuple[bytes, ...]:
        if not value:
            return ()
        if not value.endswith(b"\0"):
            raise WorkspaceTransactionError("expected NUL-delimited Git output")
        return tuple(record for record in value[:-1].split(b"\0") if record)

    @staticmethod
    def _decode_path(value: bytes) -> str:
        try:
            return value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise WorkspaceTransactionError("Git path is not stable UTF-8") from error

    @staticmethod
    def _require_snapshot(
        inventory: _Inventory,
        path: str,
        label: str,
    ) -> FileSnapshot:
        snapshot = inventory.files.get(path)
        if snapshot is None:
            raise WorkspaceTransactionError(f"{label} is absent from inventory: {path}")
        return snapshot

    def _require_baseline_state(self) -> RepositoryState:
        if self._baseline_state is None:
            raise WorkspaceTransactionError("workspace baseline state is unavailable")
        return self._baseline_state

    def _require_baseline_inventory(self) -> _Inventory:
        if self._baseline_inventory is None:
            raise WorkspaceTransactionError("workspace baseline inventory is unavailable")
        return self._baseline_inventory

    def _require_git_metadata_seal(self) -> GitMetadataSeal:
        if self._git_metadata_seal is None:
            raise WorkspaceTransactionError("workspace Git metadata seal is unavailable")
        return self._git_metadata_seal

    def _require_secure_root(self) -> SecureWorkspaceRoot:
        if self._secure_root is None:
            raise WorkspaceTransactionError("workspace root handle is unavailable")
        return self._secure_root

    def _close_secure_root(self) -> None:
        secure_root = self._secure_root
        self._secure_root = None
        if secure_root is not None:
            secure_root.close()


def materialize_workspace_preimages(
    repo: Path,
    preimages: tuple[FilePreimage, ...],
    *,
    max_paths: int = 500,
    secure_root: SecureWorkspaceRoot | None = None,
    path_policy: PathPolicy | None = None,
) -> None:
    if not 1 <= max_paths <= 50_000:
        raise ValueError("preimage path limit is invalid")
    if len(preimages) > max_paths:
        raise WorkspaceTransactionError("preimage materialization exceeds its path limit")
    by_path = {preimage.path: preimage for preimage in preimages}
    if len(by_path) != len(preimages):
        raise WorkspaceTransactionError("preimage materialization contains duplicate paths")

    owns_root = secure_root is None
    anchor = secure_root or SecureWorkspaceRoot(repo)
    try:
        anchor.assert_root_identity()
        policy = path_policy or PathPolicy(anchor.path, max_scope_files=max_paths)
        if policy.repo_root != anchor.path:
            raise WorkspaceTransactionError(
                "preimage path policy does not match the pinned workspace root"
            )
        for preimage in preimages:
            if (
                len(preimage.content) != preimage.size_bytes
                or sha256(preimage.content).hexdigest() != preimage.sha256
            ):
                raise WorkspaceTransactionError(
                    f"preimage content does not match its manifest: {preimage.path}"
                )
            validated = policy.validate_cleanup_path(preimage.path)
            try:
                anchor.replace_regular(
                    validated.relative_path,
                    preimage.content,
                    mode=preimage.mode,
                )
            except SecureFileError as error:
                raise WorkspaceRestoreError(
                    f"preimage could not be safely restored: {preimage.path}"
                ) from error
    finally:
        if owns_root:
            anchor.close()


__all__ = [
    "CapturedWorkspaceChangeSet",
    "FilePreimage",
    "FileSnapshot",
    "WorkspaceNotClean",
    "WorkspaceRestoreError",
    "WorkspaceTransaction",
    "WorkspaceTransactionError",
    "materialize_workspace_preimages",
]
