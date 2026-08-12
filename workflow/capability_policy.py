"""Closed-world policy for resources that require a capability grant."""

from __future__ import annotations

from pathlib import PurePosixPath

from path_rules import is_restricted_relative_path
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


def _parts(path: str) -> tuple[str, ...] | None:
    if not isinstance(path, str) or not path or "\\" in path:
        return None
    value = PurePosixPath(path)
    if value.is_absolute() or any(part in {"", ".", ".."} for part in value.parts):
        return None
    return tuple(part.casefold() for part in value.parts)


def eligible_actions(path: str) -> frozenset[PrivilegeAction]:
    """Return the exact actions permitted for an existing project resource."""

    if is_restricted_relative_path(path):
        return frozenset()
    parts = _parts(path)
    if parts is None:
        return frozenset()
    # Keep capability grants on the same deny-by-default credential corpus as
    # workspace scope validation.  A generic .json/.xml suffix is never
    # sufficient evidence that an existing file is safe to expose.
    name = parts[-1]
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
