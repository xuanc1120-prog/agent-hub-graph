"""Exact repo-relative path validation for workspace security boundaries."""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

_GLOB_CHARS = frozenset("*?[]")
_DOS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_SHORT_NAME_PATTERN = re.compile(r"^.+~[0-9]+(?:\..*)?$", re.IGNORECASE)
_SECRET_BASENAMES = frozenset(
    {
        ".envrc",
        ".netrc",
        ".npmrc",
        ".terraformrc",
        ".pypirc",
        ".yarnrc",
        ".yarnrc.yml",
        "_netrc",
        "application_default_credentials.json",
        "auth.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        "credentials.json",
        "credentials.toml",
        "credentials.tfrc.json",
        "gradle.properties",
        "nuget.config",
        "pip.conf",
        "pip.ini",
        "service-account.json",
        "settings-security.xml",
    }
)
_SECRET_COMPONENTS = frozenset(
    {
        ".aws",
        ".azure",
        ".cargo",
        ".docker",
        ".gradle",
        ".kube",
        ".m2",
        ".terraform.d",
        "gcloud",
    }
)
_SECRET_PATH_PREFIXES = frozenset(
    {
        (".config", "gh"),
        (".config", "glab"),
        (".config", "gcloud"),
        (".config", "pypoetry"),
        (".config", "rclone"),
    }
)
_FORBIDDEN_COMPONENTS = frozenset(
    {".agent-hub", ".aider", ".claude", ".codex", ".git", ".opencode", ".ssh"}
)
_FORBIDDEN_BASENAMES = frozenset(
    {
        ".aider.conf.yml",
        ".gitattributes",
        ".gitmodules",
        "agents.md",
        "claude.md",
        "opencode.json",
        "opencode.jsonc",
    }
)


class PathPolicyViolation(ValueError):
    """A candidate path is not safe for the declared workspace operation."""


@dataclass(frozen=True, slots=True)
class ValidatedPath:
    relative_path: str
    absolute_path: Path
    exists: bool


@dataclass(frozen=True, slots=True)
class ValidatedScope:
    existing_files: tuple[str, ...]
    new_files: tuple[str, ...]

    @property
    def all_files(self) -> tuple[str, ...]:
        return (*self.existing_files, *self.new_files)


class PathPolicy:
    """Validate exact existing/new file scopes without prefix or glob semantics."""

    def __init__(self, repo_root: Path, *, max_scope_files: int = 100) -> None:
        if max_scope_files < 1:
            raise ValueError("max_scope_files must be >= 1")
        root = repo_root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise PathPolicyViolation("repository root must be a directory")
        self._assert_not_link_or_reparse(root, label="repository root")
        self._root = root
        self._max_scope_files = max_scope_files

    @property
    def repo_root(self) -> Path:
        return self._root

    def validate_existing(self, candidate: str) -> ValidatedPath:
        normalized, parts = self._validate_shape(candidate)
        target = self._root.joinpath(*parts)
        self._assert_ancestors_safe(target, include_target=True)
        if not target.exists():
            raise PathPolicyViolation(f"existing path does not exist: {normalized}")
        if not target.is_file():
            raise PathPolicyViolation(f"existing scope must name a regular file: {normalized}")
        self._assert_contained(target.resolve(strict=True))
        if target.stat().st_nlink > 1:
            raise PathPolicyViolation(f"hard-linked files are not allowed: {normalized}")
        return ValidatedPath(normalized, target, True)

    def validate_new(self, candidate: str) -> ValidatedPath:
        normalized, parts = self._validate_shape(candidate)
        target = self._root.joinpath(*parts)
        self._assert_ancestors_safe(target, include_target=False)
        if target.exists() or target.is_symlink():
            raise PathPolicyViolation(f"new path already exists: {normalized}")
        nearest = target.parent
        while not nearest.exists():
            if nearest == self._root:
                break
            nearest = nearest.parent
        if not nearest.is_dir():
            raise PathPolicyViolation(f"new path has no safe directory ancestor: {normalized}")
        self._assert_not_link_or_reparse(nearest, label=normalized)
        self._assert_contained(nearest.resolve(strict=True))
        return ValidatedPath(normalized, target, False)

    def validate_scope(
        self,
        existing_files: Iterable[str],
        new_files: Iterable[str],
    ) -> ValidatedScope:
        existing_candidates = tuple(existing_files)
        new_candidates = tuple(new_files)
        if len(existing_candidates) + len(new_candidates) > self._max_scope_files:
            raise PathPolicyViolation(
                f"effective scope exceeds {self._max_scope_files} exact files"
            )
        normalized_seen: dict[str, str] = {}

        def register(path: str) -> None:
            key = self.comparison_key(path)
            previous = normalized_seen.get(key)
            if previous is not None:
                raise PathPolicyViolation(
                    f"scope contains duplicate or case/Unicode-equivalent paths: "
                    f"{previous!r}, {path!r}"
                )
            normalized_seen[key] = path

        existing: list[str] = []
        for candidate in existing_candidates:
            path = self.validate_existing(candidate).relative_path
            register(path)
            existing.append(path)

        new: list[str] = []
        for candidate in new_candidates:
            normalized, _parts = self._validate_shape(candidate)
            register(normalized)
            new.append(self.validate_new(normalized).relative_path)

        return ValidatedScope(
            existing_files=tuple(sorted(existing, key=self.comparison_key)),
            new_files=tuple(sorted(new, key=self.comparison_key)),
        )

    def validate_captured_path(self, candidate: str, *, must_exist: bool) -> ValidatedPath:
        if must_exist:
            return self.validate_existing(candidate)
        normalized, parts = self._validate_shape(candidate)
        target = self._root.joinpath(*parts)
        self._assert_ancestors_safe(target, include_target=target.exists())
        self._assert_contained(target.resolve(strict=False))
        return ValidatedPath(normalized, target, target.exists())

    def validate_cleanup_path(self, candidate: str) -> ValidatedPath:
        """Validate containment for exact rollback without granting task access."""
        normalized, parts = self._validate_shape(
            candidate, allow_sensitive=True, allow_control=True
        )
        target = self._root.joinpath(*parts)
        self._assert_ancestors_safe(target, include_target=target.exists() or target.is_symlink())
        self._assert_contained(target.resolve(strict=False))
        return ValidatedPath(normalized, target, target.exists())

    @staticmethod
    def comparison_key(path: str) -> str:
        normalized = unicodedata.normalize("NFC", path)
        return normalized.casefold() if os.name == "nt" else normalized

    def _validate_shape(
        self,
        candidate: str,
        *,
        allow_sensitive: bool = False,
        allow_control: bool = False,
    ) -> tuple[str, tuple[str, ...]]:
        if not isinstance(candidate, str):
            raise PathPolicyViolation("path must be a string")
        if not candidate or len(candidate) > 1_024:
            raise PathPolicyViolation("path length must be within 1..1024 characters")
        try:
            candidate.encode("utf-8", errors="strict")
        except UnicodeError as error:
            raise PathPolicyViolation("path must have a stable UTF-8 encoding") from error
        if unicodedata.normalize("NFC", candidate) != candidate:
            raise PathPolicyViolation("path must already be Unicode NFC normalized")
        if any(ord(character) < 32 or ord(character) == 127 for character in candidate):
            raise PathPolicyViolation("path contains a control character")
        if "\\" in candidate:
            raise PathPolicyViolation("repo-relative paths must use '/' separators")
        if any(character in candidate for character in _GLOB_CHARS):
            raise PathPolicyViolation("glob patterns are not allowed")
        windows_path = PureWindowsPath(candidate)
        posix_path = PurePosixPath(candidate)
        if (
            windows_path.is_absolute()
            or windows_path.drive
            or windows_path.root
            or posix_path.is_absolute()
            or candidate.startswith(("//", "\\\\"))
        ):
            raise PathPolicyViolation("absolute, drive, UNC, and device paths are not allowed")
        parts = tuple(posix_path.parts)
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise PathPolicyViolation("path must be canonical and cannot contain '.' or '..'")
        for part in parts:
            if part.endswith((" ", ".")):
                raise PathPolicyViolation("path components cannot end with a dot or space")
            if ":" in part:
                raise PathPolicyViolation("NTFS alternate data streams are not allowed")
            stem = part.split(".", 1)[0].upper()
            if stem in _DOS_RESERVED_NAMES:
                raise PathPolicyViolation(f"reserved Windows path component: {part}")
            if _SHORT_NAME_PATTERN.fullmatch(part):
                raise PathPolicyViolation(f"ambiguous Windows short-name component: {part}")
        lowered = tuple(part.casefold() for part in parts)
        if any(part in _FORBIDDEN_COMPONENTS for part in lowered):
            raise PathPolicyViolation("Git/runtime metadata and SSH paths are forbidden")
        if not allow_sensitive and any(part in _SECRET_COMPONENTS for part in lowered):
            raise PathPolicyViolation("cloud and package credential paths are forbidden")
        if not allow_sensitive and any(
            lowered[index : index + len(prefix)] == prefix
            for prefix in _SECRET_PATH_PREFIXES
            for index in range(len(lowered) - len(prefix) + 1)
        ):
            raise PathPolicyViolation("credential configuration directories are forbidden")
        basename = lowered[-1]
        if not allow_control and basename in _FORBIDDEN_BASENAMES:
            raise PathPolicyViolation("Git/Agent control files are forbidden")
        if not allow_sensitive and (
            basename == ".env"
            or basename.startswith(".env.")
            or basename.startswith(".envrc.")
            or basename in _SECRET_BASENAMES
            or basename.endswith((".pem", ".key", ".p12", ".pfx"))
        ):
            raise PathPolicyViolation("sensitive file paths are forbidden")
        normalized = "/".join(parts)
        return normalized, parts

    def _assert_ancestors_safe(self, target: Path, *, include_target: bool) -> None:
        relative = target.relative_to(self._root)
        current = self._root
        self._assert_not_link_or_reparse(current, label="repository root")
        parts = relative.parts if include_target else relative.parts[:-1]
        for part in parts:
            current = current / part
            if not current.exists() and not current.is_symlink():
                continue
            self._assert_not_link_or_reparse(current, label=str(relative))

    def _assert_contained(self, target: Path) -> None:
        try:
            common = Path(os.path.commonpath((self._root, target)))
        except ValueError as error:
            raise PathPolicyViolation("path is outside the repository") from error
        if common != self._root:
            raise PathPolicyViolation("path is outside the repository")

    @staticmethod
    def _assert_not_link_or_reparse(path: Path, *, label: str) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise PathPolicyViolation(f"symlink paths are not allowed: {label}")
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        attributes = getattr(metadata, "st_file_attributes", 0)
        if attributes & reparse_flag:
            raise PathPolicyViolation(f"reparse/junction paths are not allowed: {label}")


__all__ = [
    "PathPolicy",
    "PathPolicyViolation",
    "ValidatedPath",
    "ValidatedScope",
]
