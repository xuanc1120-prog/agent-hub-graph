"""Isolated Git operations for session integration repositories."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Collection, Sequence
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

_GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class _Digest(Protocol):
    def update(self, value: bytes, /) -> None: ...


class GitManagerError(RuntimeError):
    """A Git operation or repository invariant failed."""


class GitCommandError(GitManagerError):
    """An isolated Git subprocess returned an unexpected result."""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        self.args_vector = tuple(args)
        self.returncode = returncode
        self.stderr = stderr
        rendered = " ".join(args[:3])
        super().__init__(f"Git command failed ({returncode}): {rendered}: {stderr[:500]}")


@dataclass(frozen=True, slots=True)
class RepositoryState:
    commit: str
    branch: str
    staged: bool
    unstaged: bool
    untracked_files: tuple[str, ...]
    ignored_files: tuple[str, ...]

    @property
    def dirty(self) -> bool:
        return self.staged or self.unstaged or bool(self.untracked_files)


@dataclass(frozen=True, slots=True)
class SourceRepository:
    root: Path
    commit: str
    branch: str
    tracked_paths: tuple[str, ...]
    git_size_bytes: int
    dirty: bool


@dataclass(frozen=True, slots=True)
class GitEvidence:
    status_bytes: bytes
    staged_diff_bytes: bytes
    unstaged_diff_bytes: bytes
    changed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CanonicalPatch:
    patch_bytes: bytes
    name_status_bytes: bytes


@dataclass(frozen=True, slots=True)
class CanonicalBaselineFile:
    path: str
    content: bytes
    mode: int


@dataclass(frozen=True, slots=True)
class GitMetadataSeal:
    sha256: str
    entry_count: int


class GitManager:
    """Run a narrow set of Git commands with no user config, hooks, or prompts."""

    def __init__(
        self,
        profile_root: Path,
        *,
        git_binary: Path | None = None,
        max_tracked_paths: int = 50_000,
        max_git_bytes: int = 2 * 1024 * 1024 * 1024,
        timeout_seconds: int = 30,
    ) -> None:
        located = str(git_binary) if git_binary is not None else shutil.which("git")
        if located is None:
            raise GitManagerError("Git executable is not available")
        resolved_binary = Path(located).expanduser().resolve(strict=True)
        if not resolved_binary.is_file():
            raise GitManagerError("Git executable must resolve to a regular file")
        if max_tracked_paths < 1 or max_git_bytes < 1 or timeout_seconds < 1:
            raise ValueError("GitManager limits must be positive")
        self._git_binary = resolved_binary
        self._profile_root = profile_root.expanduser().resolve(strict=False)
        self._home = self._profile_root / "home"
        self._hooks = self._profile_root / "empty-hooks"
        self._max_tracked_paths = max_tracked_paths
        self._max_git_bytes = max_git_bytes
        self._timeout_seconds = timeout_seconds

    @property
    def git_binary(self) -> Path:
        return self._git_binary

    def inspect_source_repository(
        self,
        source_repo: Path,
        *,
        base_ref: str = "HEAD",
    ) -> SourceRepository:
        root = source_repo.expanduser().resolve(strict=True)
        self._assert_directory_not_reparse(root, label="source repository")
        if not self._valid_base_ref(base_ref):
            raise GitManagerError("base_ref is invalid or option-like")
        top_level = Path(
            self._decode(self._run(("rev-parse", "--show-toplevel"), cwd=root).stdout).strip()
        ).resolve(strict=True)
        if top_level != root:
            raise GitManagerError("source path must be the repository top level")
        commit = self._decode(
            self._run(
                ("rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}"),
                cwd=root,
            ).stdout
        ).strip()
        if not _GIT_OBJECT_ID.fullmatch(commit):
            raise GitManagerError("resolved base_ref is not a supported Git object id")
        state = self.state(root)
        tracked_paths = self._nul_paths(self._run(("ls-files", "-z", "--"), cwd=root).stdout)
        if len(tracked_paths) > self._max_tracked_paths:
            raise GitManagerError(
                f"source repository exceeds {self._max_tracked_paths} tracked paths"
            )
        self._reject_unsupported_repository(root, commit, tracked_paths)
        git_size_bytes = self._git_object_size(root)
        if git_size_bytes > self._max_git_bytes:
            raise GitManagerError(f"source Git objects exceed {self._max_git_bytes} bytes")
        return SourceRepository(
            root=root,
            commit=commit,
            branch=state.branch,
            tracked_paths=tracked_paths,
            git_size_bytes=git_size_bytes,
            dirty=state.dirty,
        )

    def create_session_repository(
        self,
        *,
        source: SourceRepository,
        destination: Path,
        session_id: str,
    ) -> RepositoryState:
        branch = f"agent-hub/session/{session_id}"
        self._run(("check-ref-format", "--branch", branch), cwd=source.root)
        target = destination.expanduser().resolve(strict=False)
        parent = target.parent
        if target.exists() or target.is_symlink():
            raise GitManagerError("session repository destination already exists")
        if parent.exists():
            raise GitManagerError("session workspace directory already exists")
        parent.mkdir(parents=False, exist_ok=False)
        try:
            self._run(
                (
                    "clone",
                    "--local",
                    "--no-hardlinks",
                    "--no-checkout",
                    "--no-tags",
                    "--",
                    str(source.root),
                    str(target),
                ),
                cwd=parent,
                timeout_seconds=max(self._timeout_seconds, 120),
            )
            self._assert_directory_not_reparse(target, label="session repository")
            self._run(
                (
                    "checkout",
                    "--no-recurse-submodules",
                    "-b",
                    branch,
                    source.commit,
                    "--",
                ),
                cwd=target,
                timeout_seconds=max(self._timeout_seconds, 120),
            )
            remotes = self._lines(self._run(("remote",), cwd=target).stdout)
            for remote in remotes:
                if remote.startswith("-") or "\n" in remote or "\r" in remote:
                    raise GitManagerError("clone produced an invalid remote name")
                self._run(("remote", "remove", remote), cwd=target)
            if self._lines(self._run(("remote",), cwd=target).stdout):
                raise GitManagerError("session repository must not retain remotes")
            self._reject_source_path_leak(target, source.root)
            self._reject_hardlinked_objects(target)
            state = self.state(target)
            if state.commit != source.commit or state.branch != branch or state.dirty:
                raise GitManagerError("session repository failed post-clone verification")
            return state
        except BaseException:
            self.remove_session_repository(target, allowed_root=parent.parent)
            raise

    def state(self, repo: Path) -> RepositoryState:
        root = repo.expanduser().resolve(strict=True)
        self._assert_directory_not_reparse(root, label="repository")
        commit = self._decode(
            self._run(
                ("rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"),
                cwd=root,
            ).stdout
        ).strip()
        if not _GIT_OBJECT_ID.fullmatch(commit):
            raise GitManagerError("repository HEAD is not a supported Git object id")
        branch_result = self._run(
            ("symbolic-ref", "--quiet", "--short", "HEAD"),
            cwd=root,
            allowed_returncodes={0, 1},
        )
        branch = self._decode(branch_result.stdout).strip()
        staged = (
            self._run(
                ("diff", "--cached", "--quiet", "--no-ext-diff", "--no-textconv", "--"),
                cwd=root,
                allowed_returncodes={0, 1},
            ).returncode
            == 1
        )
        unstaged = (
            self._run(
                ("diff", "--quiet", "--no-ext-diff", "--no-textconv", "--"),
                cwd=root,
                allowed_returncodes={0, 1},
            ).returncode
            == 1
        )
        untracked = self._nul_paths(
            self._run(("ls-files", "--others", "--exclude-standard", "-z", "--"), cwd=root).stdout
        )
        ignored = self._nul_paths(
            self._run(
                ("ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--"),
                cwd=root,
            ).stdout
        )
        return RepositoryState(
            commit=commit,
            branch=branch,
            staged=staged,
            unstaged=unstaged,
            untracked_files=untracked,
            ignored_files=ignored,
        )

    def capture_evidence(self, repo: Path) -> GitEvidence:
        root = repo.expanduser().resolve(strict=True)
        status = self._run(
            (
                "status",
                "--porcelain=v2",
                "-z",
                "--untracked-files=all",
                "--ignored=matching",
                "--",
            ),
            cwd=root,
        ).stdout
        staged_diff = self._run(
            (
                "diff",
                "--cached",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                "--no-textconv",
                "--",
            ),
            cwd=root,
        ).stdout
        unstaged_diff = self._run(
            (
                "diff",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                "--no-textconv",
                "--",
            ),
            cwd=root,
        ).stdout
        staged_paths = self._nul_paths(
            self._run(("diff", "--cached", "--name-only", "-z", "--"), cwd=root).stdout
        )
        unstaged_paths = self._nul_paths(
            self._run(("diff", "--name-only", "-z", "--"), cwd=root).stdout
        )
        untracked_paths = self._nul_paths(
            self._run(
                ("ls-files", "--others", "--exclude-standard", "-z", "--"),
                cwd=root,
            ).stdout
        )
        changed_paths = tuple(sorted({*staged_paths, *unstaged_paths, *untracked_paths}))
        return GitEvidence(
            status_bytes=status,
            staged_diff_bytes=staged_diff,
            unstaged_diff_bytes=unstaged_diff,
            changed_paths=changed_paths,
        )

    def tracked_paths(self, repo: Path) -> tuple[str, ...]:
        return self._nul_paths(self._run(("ls-files", "-z", "--"), cwd=repo).stdout)

    def read_blob(self, repo: Path, *, commit: str, path: str) -> bytes:
        self._require_object_id(commit)
        self._pathspec_bytes((path,))
        return self._run(("cat-file", "blob", f"{commit}:{path}"), cwd=repo).stdout

    def build_canonical_patch(
        self,
        repo: Path,
        *,
        base_commit: str,
        changed_paths: Sequence[str],
        temp_directory: Path,
        baseline_files: Sequence[CanonicalBaselineFile] = (),
    ) -> CanonicalPatch:
        self._require_object_id(base_commit)
        root = repo.expanduser().resolve(strict=True)
        paths = tuple(sorted(set(changed_paths)))
        if not paths:
            return CanonicalPatch(patch_bytes=b"", name_status_bytes=b"")
        pathspec = self._pathspec_bytes(paths)
        baseline_by_path = {item.path: item for item in baseline_files}
        if len(baseline_by_path) != len(baseline_files):
            raise GitManagerError("canonical baseline file paths must be unique")
        self._pathspec_bytes(tuple(baseline_by_path))
        for item in baseline_files:
            if item.mode < 0 or item.mode > 0o777:
                raise GitManagerError("canonical baseline file mode is invalid")
        temporary_root = temp_directory.expanduser().resolve(strict=False)
        try:
            temporary_common = Path(os.path.commonpath((root, temporary_root)))
        except ValueError:
            temporary_common = None
        if temporary_common == root:
            raise GitManagerError("temporary Git state must be outside the session repository")
        temporary_root.mkdir(parents=True, exist_ok=True)
        self._assert_directory_not_reparse(temporary_root, label="temporary Git state root")
        operation_root = temporary_root / f"canonical-{uuid.uuid4().hex}"
        operation_root.mkdir(parents=False, exist_ok=False)
        index_path = operation_root / "index"
        object_directory = Path(tempfile.mkdtemp(prefix="ah-git-objects-")).resolve(strict=True)
        try:
            self._assert_directory_not_reparse(
                object_directory,
                label="temporary Git object store",
            )
            try:
                object_common = Path(os.path.commonpath((root, object_directory)))
            except ValueError:
                object_common = None
            if object_common == root:
                raise GitManagerError(
                    "temporary Git object store must be outside the session repository"
                )
            alternate_objects = (root / ".git" / "objects").resolve(strict=True)
            before_index = self.index_sha256(root)
            environment = {
                "GIT_INDEX_FILE": str(index_path),
                "GIT_OBJECT_DIRECTORY": str(object_directory),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(alternate_objects),
            }
            self._run(
                ("read-tree", base_commit),
                cwd=root,
                extra_environment=environment,
            )
            for item in sorted(baseline_files, key=lambda value: value.path):
                object_id = self._decode(
                    self._run(
                        ("hash-object", "-w", "--stdin"),
                        cwd=root,
                        input_bytes=item.content,
                        extra_environment=environment,
                    ).stdout
                ).strip()
                self._require_object_id(object_id)
                git_mode = "100755" if item.mode & 0o111 else "100644"
                self._run(
                    (
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        f"{git_mode},{object_id},{item.path}",
                    ),
                    cwd=root,
                    extra_environment=environment,
                )
            baseline_tree = self._decode(
                self._run(
                    ("write-tree",),
                    cwd=root,
                    extra_environment=environment,
                ).stdout
            ).strip()
            self._require_object_id(baseline_tree)
            self._run(
                (
                    "add",
                    "-A",
                    "-f",
                    "--pathspec-from-file=-",
                    "--pathspec-file-nul",
                ),
                cwd=root,
                input_bytes=pathspec,
                extra_environment=environment,
            )
            patch = self._run(
                (
                    "diff",
                    "--cached",
                    "--binary",
                    "--full-index",
                    "--find-renames",
                    "--no-ext-diff",
                    "--no-textconv",
                    baseline_tree,
                    "--",
                ),
                cwd=root,
                extra_environment=environment,
            ).stdout
            name_status = self._run(
                (
                    "diff",
                    "--cached",
                    "--name-status",
                    "-z",
                    "--find-renames",
                    baseline_tree,
                    "--",
                ),
                cwd=root,
                extra_environment=environment,
            ).stdout
        finally:
            try:
                self._remove_tree(operation_root)
            finally:
                self._remove_tree(object_directory)
        if self.index_sha256(root) != before_index:
            raise GitManagerError("canonical patch construction modified the real Git index")
        return CanonicalPatch(patch_bytes=patch, name_status_bytes=name_status)

    def apply_check(self, repo: Path, patch_bytes: bytes) -> None:
        self._run(
            ("apply", "--check", "--binary", "--whitespace=nowarn", "-"),
            cwd=repo,
            input_bytes=patch_bytes,
        )

    def apply_patch(self, repo: Path, patch_bytes: bytes) -> None:
        self._run(
            ("apply", "--binary", "--whitespace=nowarn", "-"),
            cwd=repo,
            input_bytes=patch_bytes,
        )

    def restore_existing_paths(
        self,
        repo: Path,
        *,
        base_commit: str,
        paths: Sequence[str],
    ) -> None:
        self._require_object_id(base_commit)
        if not paths:
            return
        self._run(
            (
                "restore",
                f"--source={base_commit}",
                "--staged",
                "--worktree",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
            ),
            cwd=repo,
            input_bytes=self._pathspec_bytes(paths),
        )

    def unstage_new_paths(self, repo: Path, paths: Sequence[str]) -> None:
        if not paths:
            return
        self._run(
            (
                "rm",
                "--cached",
                "-f",
                "--ignore-unmatch",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
            ),
            cwd=repo,
            input_bytes=self._pathspec_bytes(paths),
        )

    def index_sha256(self, repo: Path) -> str:
        root = repo.expanduser().resolve(strict=True)
        entries = self._run(("ls-files", "--stage", "-z", "--"), cwd=root).stdout
        return sha256(entries).hexdigest()

    def capture_metadata_seal(
        self,
        repo: Path,
        *,
        include_objects: bool = False,
    ) -> GitMetadataSeal:
        digest, entry_count = self._git_metadata_digest(
            repo,
            include_objects=include_objects,
        )
        return GitMetadataSeal(sha256=digest, entry_count=entry_count)

    def assert_metadata_seal(
        self,
        repo: Path,
        expected: GitMetadataSeal,
        *,
        include_objects: bool = False,
    ) -> None:
        current = self.capture_metadata_seal(repo, include_objects=include_objects)
        if current != expected:
            raise GitManagerError("session Git control metadata changed during task execution")

    def commit_validation_baseline(self, repo: Path) -> RepositoryState:
        root = repo.expanduser().resolve(strict=True)
        self._run(("add", "-A", "-f", "--"), cwd=root)
        self._run(
            ("commit", "--no-verify", "--no-gpg-sign", "-m", "Agent Hub validation baseline"),
            cwd=root,
            extra_environment={
                "GIT_AUTHOR_NAME": "Agent Hub",
                "GIT_AUTHOR_EMAIL": "agent-hub@localhost.invalid",
                "GIT_COMMITTER_NAME": "Agent Hub",
                "GIT_COMMITTER_EMAIL": "agent-hub@localhost.invalid",
                "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
                "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
            },
        )
        state = self.state(root)
        if state.dirty:
            raise GitManagerError("validation baseline commit left a dirty repository")
        return state

    def remove_session_repository(self, destination: Path, *, allowed_root: Path) -> None:
        target = destination.expanduser().resolve(strict=False)
        workspace = target.parent
        root = allowed_root.expanduser().resolve(strict=True)
        try:
            common = Path(os.path.commonpath((root, workspace)))
        except ValueError as error:
            raise GitManagerError("session cleanup path is outside the workspace root") from error
        if common != root or workspace == root:
            raise GitManagerError("session cleanup path is outside the workspace root")
        if workspace.exists() or workspace.is_symlink():
            self._remove_tree(workspace)

    @staticmethod
    def _remove_tree(root: Path) -> None:
        if not root.exists() and not root.is_symlink():
            return
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

        def is_link_or_reparse(path: Path) -> tuple[bool, os.stat_result]:
            metadata = path.lstat()
            return (
                stat.S_ISLNK(metadata.st_mode)
                or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag),
                metadata,
            )

        root_is_link, root_metadata = is_link_or_reparse(root)
        if root_is_link:
            if stat.S_ISLNK(root_metadata.st_mode):
                root.unlink()
            elif stat.S_ISDIR(root_metadata.st_mode):
                root.rmdir()
            else:
                root.unlink()
            return
        with suppress(OSError):
            root.chmod(stat.S_IRWXU)
        for current_root, directories, files in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            current = Path(current_root)
            safe_directories: list[str] = []
            for name in directories:
                target = current / name
                linked, metadata = is_link_or_reparse(target)
                if linked:
                    if stat.S_ISLNK(metadata.st_mode):
                        target.unlink()
                    elif stat.S_ISDIR(metadata.st_mode):
                        target.rmdir()
                    else:
                        target.unlink()
                    continue
                with suppress(OSError):
                    target.chmod(stat.S_IRWXU)
                safe_directories.append(name)
            directories[:] = safe_directories
            for name in files:
                target = current / name
                linked, _metadata = is_link_or_reparse(target)
                if linked:
                    target.unlink()
                else:
                    with suppress(OSError):
                        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
        shutil.rmtree(root)

    def _run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        input_bytes: bytes | None = None,
        extra_environment: dict[str, str] | None = None,
        allowed_returncodes: Collection[int] = (0,),
        timeout_seconds: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        self._prepare_profile()
        environment = self._environment()
        environment.update(extra_environment or {})
        command = [
            str(self._git_binary),
            "--no-pager",
            "-c",
            f"core.hooksPath={self._hooks}",
            "-c",
            "credential.helper=",
            "-c",
            "core.askPass=",
            "-c",
            "core.pager=cat",
            "-c",
            "pager.branch=false",
            "-c",
            "diff.external=",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "tag.gpgSign=false",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.safecrlf=false",
            "-c",
            "core.quotepath=false",
            *args,
        ]
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            input=input_bytes,
            capture_output=True,
            shell=False,
            timeout=timeout_seconds or self._timeout_seconds,
            check=False,
        )
        if completed.returncode not in allowed_returncodes:
            raise GitCommandError(
                tuple(args),
                completed.returncode,
                self._decode(completed.stderr, errors="replace").strip(),
            )
        return completed

    def _prepare_profile(self) -> None:
        self._home.mkdir(parents=True, exist_ok=True)
        self._hooks.mkdir(parents=True, exist_ok=True)

    def _environment(self) -> dict[str, str]:
        environment = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "GCM_INTERACTIVE": "Never",
            "SSH_ASKPASS_REQUIRE": "never",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "HOME": str(self._home),
            "XDG_CONFIG_HOME": str(self._home / ".config"),
            "USERPROFILE": str(self._home),
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
        }
        for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        environment["PATH"] = str(self._git_binary.parent)
        return environment

    def _reject_unsupported_repository(
        self,
        repo: Path,
        commit: str,
        tracked_paths: tuple[str, ...],
    ) -> None:
        stage_records = self._nul_records(
            self._run(("ls-files", "--stage", "-z", "--"), cwd=repo).stdout
        )
        if any(record.startswith(b"160000 ") for record in stage_records):
            raise GitManagerError("submodule gitlinks are not supported")
        if any(record.startswith(b"120000 ") for record in stage_records):
            raise GitManagerError("tracked symlinks are not supported")
        casefolded = {path.casefold() for path in tracked_paths}
        if ".gitmodules" in casefolded:
            raise GitManagerError(".gitmodules repositories are not supported")
        if ".lfsconfig" in casefolded:
            raise GitManagerError("Git LFS configuration is not supported")
        attribute_paths = [
            path for path in tracked_paths if path.casefold().endswith(".gitattributes")
        ]
        for attribute_path in attribute_paths:
            blob = self._run(
                ("show", "--no-ext-diff", "--no-textconv", f"{commit}:{attribute_path}"),
                cwd=repo,
            ).stdout
            lowered = blob.lower()
            if b"filter=lfs" in lowered or re.search(rb"(?:^|\s)filter\s*=", lowered):
                raise GitManagerError("required Git content filters are not supported")

    def _git_object_size(self, repo: Path) -> int:
        output = self._decode(self._run(("count-objects", "-v"), cwd=repo).stdout)
        values: dict[str, int] = {}
        for line in output.splitlines():
            key, separator, value = line.partition(": ")
            if separator and value.isdigit():
                values[key] = int(value)
        return (values.get("size", 0) + values.get("size-pack", 0)) * 1024

    def _reject_source_path_leak(self, repo: Path, source: Path) -> None:
        config = repo / ".git" / "config"
        text = config.read_text(encoding="utf-8", errors="replace")
        variants = {str(source), str(source).replace("\\", "/")}
        if any(variant and variant in text for variant in variants):
            raise GitManagerError("session repository still contains the source path")

    def _git_metadata_digest(
        self,
        repo: Path,
        *,
        include_objects: bool,
    ) -> tuple[str, int]:
        root = repo.expanduser().resolve(strict=True)
        self._assert_directory_not_reparse(root, label="repository")
        git_dir = root / ".git"
        self._assert_directory_not_reparse(git_dir, label="session Git metadata")
        objects = git_dir / "objects"
        self._assert_directory_not_reparse(objects, label="session Git object store")
        self._reject_hardlinked_objects(root)

        digest = sha256()
        entry_count, total_bytes = self._update_stable_tree_digest(
            digest,
            git_dir,
            prefix="",
            excluded_top_directories=frozenset({"objects"}),
            mutable_files=frozenset({"index"}),
        )
        if include_objects:
            object_entries, object_bytes = self._update_stable_tree_digest(
                digest,
                objects,
                prefix="objects",
                max_bytes=self._max_git_bytes,
                limit_label="session Git object store",
            )
            entry_count += object_entries
            total_bytes += object_bytes
        else:
            objects_info = objects / "info"
            digest.update(
                b"objects-info-present\0" if objects_info.exists() else b"objects-info-absent\0"
            )
            if objects_info.exists():
                info_entries, info_bytes = self._update_stable_tree_digest(
                    digest,
                    objects_info,
                    prefix="objects/info",
                )
                entry_count += info_entries
                total_bytes += info_bytes
        if total_bytes > 16 * 1024 * 1024 and not include_objects:
            raise GitManagerError("session Git control metadata exceeds 16 MiB")
        return digest.hexdigest(), entry_count

    def _update_stable_tree_digest(
        self,
        digest: _Digest,
        root: Path,
        *,
        prefix: str,
        excluded_top_directories: frozenset[str] = frozenset(),
        mutable_files: frozenset[str] = frozenset(),
        max_bytes: int = 16 * 1024 * 1024,
        limit_label: str = "session Git control metadata",
    ) -> tuple[int, int]:
        entry_count = 0
        total_bytes = 0
        for current_root, directories, files in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            current = Path(current_root)
            safe_directories: list[str] = []
            for name in sorted(directories):
                path = current / name
                self._assert_plain_git_entry(path, expect_directory=True)
                if current == root and name in excluded_top_directories:
                    continue
                safe_directories.append(name)
                relative = path.relative_to(root).as_posix()
                sealed_path = f"{prefix}/{relative}" if prefix else relative
                digest.update(b"D\0")
                digest.update(sealed_path.encode("utf-8", errors="strict"))
                digest.update(b"\0")
                entry_count += 1
            directories[:] = safe_directories
            for name in sorted(files):
                path = current / name
                self._assert_plain_git_entry(path, expect_directory=False)
                relative = path.relative_to(root).as_posix()
                if current == root and relative in mutable_files:
                    continue
                sealed_path = f"{prefix}/{relative}" if prefix else relative
                if name.endswith(".lock"):
                    raise GitManagerError(
                        f"session Git metadata contains a stale lock: {sealed_path}"
                    )
                content = path.read_bytes()
                total_bytes += len(content)
                if total_bytes > max_bytes:
                    raise GitManagerError(f"{limit_label} exceeds its byte limit")
                digest.update(b"F\0")
                digest.update(sealed_path.encode("utf-8", errors="strict"))
                digest.update(b"\0")
                digest.update(str(len(content)).encode("ascii"))
                digest.update(b"\0")
                digest.update(sha256(content).digest())
                entry_count += 1
        return entry_count, total_bytes

    @staticmethod
    def _assert_plain_git_entry(path: Path, *, expect_directory: bool) -> None:
        metadata = path.lstat()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0) & reparse_flag
        ):
            raise GitManagerError("session Git metadata contains a link/reparse point")
        if expect_directory:
            if not stat.S_ISDIR(metadata.st_mode):
                raise GitManagerError("session Git metadata directory is not regular")
        elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
            raise GitManagerError("session Git metadata file is not plain and single-link")

    @staticmethod
    def _reject_hardlinked_objects(repo: Path) -> None:
        objects = repo / ".git" / "objects"
        for root, directories, files in os.walk(objects, followlinks=False):
            safe_directories: list[str] = []
            for name in directories:
                path = Path(root) / name
                metadata = path.lstat()
                reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or getattr(metadata, "st_file_attributes", 0) & reparse_flag
                ):
                    raise GitManagerError("session Git objects contain a link/reparse point")
                if not stat.S_ISDIR(metadata.st_mode):
                    raise GitManagerError("session Git object directory is not regular")
                safe_directories.append(name)
            directories[:] = safe_directories
            for name in files:
                path = Path(root) / name
                metadata = path.lstat()
                reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or getattr(metadata, "st_file_attributes", 0) & reparse_flag
                    or not stat.S_ISREG(metadata.st_mode)
                ):
                    raise GitManagerError("session Git objects contain a non-regular file")
                if metadata.st_nlink > 1:
                    raise GitManagerError("session repository contains hard-linked Git objects")

    @staticmethod
    def _assert_directory_not_reparse(path: Path, *, label: str) -> None:
        metadata = path.lstat()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & reparse_flag:
            raise GitManagerError(f"{label} cannot be a symlink, junction, or reparse point")
        if not path.is_dir():
            raise GitManagerError(f"{label} must be a directory")

    @staticmethod
    def _require_object_id(value: str) -> None:
        if not _GIT_OBJECT_ID.fullmatch(value):
            raise GitManagerError("invalid Git object id")

    @staticmethod
    def _pathspec_bytes(paths: Sequence[str]) -> bytes:
        encoded: list[bytes] = []
        for path in paths:
            if not isinstance(path, str) or not path or "\0" in path:
                raise GitManagerError("pathspec entries must be non-empty NUL-free strings")
            try:
                encoded.append(path.encode("utf-8", errors="strict") + b"\0")
            except UnicodeEncodeError as error:
                raise GitManagerError("pathspec is not stable UTF-8") from error
        return b"".join(encoded)

    @staticmethod
    def _valid_base_ref(base_ref: str) -> bool:
        return (
            isinstance(base_ref, str)
            and 1 <= len(base_ref) <= 256
            and not base_ref.startswith("-")
            and not any(ord(character) < 32 or ord(character) == 127 for character in base_ref)
        )

    @staticmethod
    def _decode(value: bytes, *, errors: str = "strict") -> str:
        try:
            return value.decode("utf-8", errors=errors)
        except UnicodeDecodeError as error:
            raise GitManagerError("Git output is not stable UTF-8") from error

    @classmethod
    def _nul_paths(cls, value: bytes) -> tuple[str, ...]:
        return tuple(cls._decode(record) for record in cls._nul_records(value))

    @staticmethod
    def _nul_records(value: bytes) -> tuple[bytes, ...]:
        if not value:
            return ()
        if not value.endswith(b"\0"):
            raise GitManagerError("expected NUL-delimited Git output")
        return tuple(record for record in value[:-1].split(b"\0") if record)

    @classmethod
    def _lines(cls, value: bytes) -> tuple[str, ...]:
        return tuple(line for line in cls._decode(value).splitlines() if line)


__all__ = [
    "CanonicalBaselineFile",
    "CanonicalPatch",
    "GitCommandError",
    "GitEvidence",
    "GitManager",
    "GitManagerError",
    "GitMetadataSeal",
    "RepositoryState",
    "SourceRepository",
]
