"""Closed-world policy for resources that require a capability grant."""

from __future__ import annotations

from pathlib import PurePosixPath

from protocol import PrivilegeAction

_DEPENDENCY_NAMES = frozenset(
    {
        "cargo.toml",
        "cargo.lock",
        "composer.json",
        "composer.lock",
        "go.mod",
        "go.sum",
        "gemfile",
        "gemfile.lock",
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pyproject.toml",
        "requirements.txt",
        "pipfile",
        "pipfile.lock",
        "yarn.lock",
    }
)
_CONFIG_SUFFIXES = frozenset(
    {
        ".cfg",
        ".conf",
        ".config",
        ".csproj",
        ".ini",
        ".json",
        ".props",
        ".targets",
        ".toml",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_SECRET_MARKERS = frozenset(
    {".env", "credentials", "credential", "secret", "secrets", "private", "token", "tokens"}
)


def _parts(path: str) -> tuple[str, ...] | None:
    if not isinstance(path, str) or not path or "\\" in path:
        return None
    value = PurePosixPath(path)
    if value.is_absolute() or any(part in {"", ".", ".."} for part in value.parts):
        return None
    return tuple(part.casefold() for part in value.parts)


def eligible_actions(path: str) -> frozenset[PrivilegeAction]:
    """Return the exact actions permitted for an existing project resource."""

    parts = _parts(path)
    if parts is None:
        return frozenset()
    name = parts[-1]
    if any(marker in parts or name == marker for marker in _SECRET_MARKERS):
        return frozenset()
    actions: set[PrivilegeAction] = set()
    if (
        name in _DEPENDENCY_NAMES
        or (name.startswith("requirements") and name.endswith((".txt", ".in")))
        or name.endswith((".csproj", ".fsproj", ".vbproj"))
    ):
        actions.add(PrivilegeAction.EDIT_DEPENDENCY_MANIFEST)
    if name.endswith(tuple(_CONFIG_SUFFIXES)) or name in {
        "editorconfig",
        "makefile",
        "dockerfile",
        "tox.ini",
        "setup.cfg",
    }:
        actions.add(PrivilegeAction.EDIT_PROJECT_CONFIG)
    return frozenset(actions)


def is_eligible_resource(action: PrivilegeAction, path: str) -> bool:
    return action in eligible_actions(path)


__all__ = ["eligible_actions", "is_eligible_resource"]
