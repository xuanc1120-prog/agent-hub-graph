"""Capture a complete task ChangeSet and restore the shared repo exactly."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from security.path_policy import PathPolicy
from workspace.change_set import ChangeSetManifest, FileAction, FileChange
from workspace.git_manager import (
    CanonicalBaselineFile,
    GitManager,
    GitManagerError,
    GitMetadataSeal,
    RepositoryState,
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

    def begin(self) -> None:
        if self._phase != "new":
            raise WorkspaceTransactionError("workspace transaction can only begin once")
        metadata_seal = self._git.capture_metadata_seal(
            self._repo,
            include_objects=self._seal_git_objects,
        )
        state = self._git.state(self._repo)
        if state.commit != self._base_commit:
            raise WorkspaceNotClean("workspace HEAD does not match the task base commit")
        if self._expected_branch is not None and state.branch != self._expected_branch:
            raise WorkspaceNotClean(
                "workspace branch does not match the session integration branch"
            )
        if state.dirty:
            raise WorkspaceNotClean("workspace must be clean before task execution")
        inventory = self._scan_inventory()
        tracked = frozenset(self._git.tracked_paths(self._repo))
        ignored = frozenset(state.ignored_files)
        if len(ignored) > self._max_changed_paths:
            raise WorkspaceNotClean("ignored baseline exceeds the replayable workspace path limit")
        preimages = self._capture_ignored_preimages(ignored, inventory)
        self._baseline_state = state
        self._baseline_inventory = inventory
        self._baseline_tracked = tracked
        self._baseline_ignored = ignored
        self._ignored_preimages = preimages
        self._baseline_index_sha256 = self._git.index_sha256(self._repo)
        self._git_metadata_seal = metadata_seal
        self._phase = "active"

    @property
    def baseline_state_hash(self) -> str:
        return self._require_baseline_inventory().state_hash

    def capture_and_restore(self) -> CapturedWorkspaceChangeSet:
        if self._phase != "active":
            raise WorkspaceTransactionError("workspace transaction is not active")
        baseline_state = self._require_baseline_state()
        baseline = self._require_baseline_inventory()
        metadata_seal = self._require_git_metadata_seal()
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
        restore_paths.update(ignored_candidates)
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
            ignored_touched = self._ignored_changes(after, current_state)
            restore_paths.difference_update(ignored_candidates)
            restore_paths.update(ignored_touched)
            self._validate_changed_paths(restore_paths, after)
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
                    for preimage in self._ignored_preimages.values()
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
                pre_state_hash=baseline.state_hash,
                post_state_hash=after.state_hash,
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
                preimages=self._build_preimages(changes),
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
            self._verify_canonical_patch(result, after)
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
        replay_id = uuid.uuid4().hex
        replay_root = Path(tempfile.gettempdir()).expanduser().resolve(strict=True)
        try:
            replay_common = Path(os.path.commonpath((self._repo, replay_root)))
        except ValueError:
            replay_common = None
        if replay_common == self._repo:
            raise WorkspaceTransactionError("replay root must be outside the session repository")
        replay_repo = replay_root / f"ah-replay-{replay_id}" / "repo"
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
                tuple(self._ignored_preimages.values()),
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
            if applied != post_inventory:
                raise WorkspaceTransactionError(
                    "canonical replay inventory differs from captured inventory"
                )
        finally:
            replay_workspace = replay_repo.parent
            if replay_workspace.exists() or replay_workspace.is_symlink():
                self._git.remove_session_repository(
                    replay_repo,
                    allowed_root=replay_root,
                )

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
            target = validated.absolute_path
            if target.exists():
                try:
                    target.rmdir()
                except OSError as error:
                    raise WorkspaceRestoreError(
                        f"created directory is not empty: {directory}"
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
        if self._scan_inventory() != baseline_inventory:
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

    def _scan_inventory(self, *, repo: Path | None = None) -> _Inventory:
        root = self._repo if repo is None else repo.expanduser().resolve(strict=True)
        files: dict[str, FileSnapshot] = {}
        directories: list[str] = []
        total_bytes = 0
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
                snapshot = self._snapshot_file(child, relative)
                files[relative] = snapshot
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
        )

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
            validated = self._path_policy.validate_cleanup_path(path)
            content = validated.absolute_path.read_bytes()
            total += len(content)
            if total > self._max_ignored_preimage_bytes:
                raise WorkspaceNotClean("ignored preimages exceed the workspace baseline limit")
            preimages[path] = FilePreimage(
                path=path,
                sha256=snapshot.sha256,
                size_bytes=snapshot.size_bytes,
                mode=snapshot.mode,
                content=content,
                baseline_ignored=True,
            )
        return preimages

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

    def _build_preimages(self, changes: tuple[FileChange, ...]) -> tuple[FilePreimage, ...]:
        preimages = dict(self._ignored_preimages)
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
        target = validated.absolute_path
        if not target.exists() and not target.is_symlink():
            return
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
            raise WorkspaceRestoreError(f"new path is not a plain single-link file: {path}")
        target.unlink()

    def _restore_preimage(self, preimage: FilePreimage) -> None:
        materialize_workspace_preimages(
            self._repo,
            (preimage,),
            max_paths=self._max_changed_paths,
        )

    @staticmethod
    def _snapshot_file(path: Path, relative: str) -> FileSnapshot:
        digest = sha256()
        size = 0
        binary = False
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                if size == 0 and b"\0" in chunk[:8192]:
                    binary = True
                digest.update(chunk)
                size += len(chunk)
        return FileSnapshot(
            path=relative,
            sha256=digest.hexdigest(),
            size_bytes=size,
            mode=stat.S_IMODE(path.stat().st_mode),
            binary=binary,
        )

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


def materialize_workspace_preimages(
    repo: Path,
    preimages: tuple[FilePreimage, ...],
    *,
    max_paths: int = 500,
) -> None:
    if not 1 <= max_paths <= 50_000:
        raise ValueError("preimage path limit is invalid")
    if len(preimages) > max_paths:
        raise WorkspaceTransactionError("preimage materialization exceeds its path limit")
    by_path = {preimage.path: preimage for preimage in preimages}
    if len(by_path) != len(preimages):
        raise WorkspaceTransactionError("preimage materialization contains duplicate paths")
    root = repo.expanduser().resolve(strict=True)
    policy = PathPolicy(root, max_scope_files=max_paths)
    for preimage in preimages:
        if (
            len(preimage.content) != preimage.size_bytes
            or sha256(preimage.content).hexdigest() != preimage.sha256
        ):
            raise WorkspaceTransactionError(
                f"preimage content does not match its manifest: {preimage.path}"
            )
        validated = policy.validate_cleanup_path(preimage.path)
        target = validated.absolute_path
        target.parent.mkdir(parents=True, exist_ok=True)
        validated = policy.validate_cleanup_path(preimage.path)
        target = validated.absolute_path
        if target.exists() or target.is_symlink():
            metadata = target.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
                raise WorkspaceRestoreError(
                    f"preimage path is not a plain single-link file: {preimage.path}"
                )
        temporary = target.parent / f".{target.name}.agent-hub-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(preimage.content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, preimage.mode)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


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
