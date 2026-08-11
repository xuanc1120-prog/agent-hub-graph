"""Dependency-neutral path rules shared by workspace and capability gates."""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath, PureWindowsPath

_GLOB_CHARS = frozenset("*?[]")
_DOS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_SHORT_NAME_PATTERN = re.compile(r"^.+~[0-9]+(?:\..*)?$", re.IGNORECASE)
_CAMEL_BOUNDARY_1 = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_BOUNDARY_2 = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")

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
        "api-key.json",
        "apikey.json",
        "apikeys.json",
        "access-token.json",
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
        "jwt.json",
        "oauth.json",
        "password.json",
        "passwords.json",
        "private.json",
        "secret.json",
        "secrets.json",
        "service-account.json",
        "settings-security.xml",
        "token.json",
        "tokens.json",
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
_SENSITIVE_NAME_WORDS = frozenset(
    {
        "access",
        "auth",
        "credential",
        "credentials",
        "key",
        "pass",
        "passwd",
        "password",
        "private",
        "pwd",
        "secret",
        "secrets",
        "security",
        "token",
        "tokens",
    }
)
_SENSITIVE_DIRECTORY_TERMINALS = frozenset(
    {
        "access",
        "credential",
        "credentials",
        "key",
        "pass",
        "passwd",
        "password",
        "private",
        "secret",
        "secrets",
        "token",
        "tokens",
    }
)
_SENSITIVE_NAME_SEQUENCES = frozenset(
    {
        ("service", "account"),
        ("settings", "security"),
    }
)


def relative_parts(candidate: str) -> tuple[str, ...] | None:
    """Return normalized repo-relative parts, or ``None`` for an invalid path."""

    if not isinstance(candidate, str) or not candidate or "\\" in candidate:
        return None
    if any(ord(char) < 32 for char in candidate) or any(char in _GLOB_CHARS for char in candidate):
        return None
    if unicodedata.normalize("NFC", candidate) != candidate:
        return None
    value = PurePosixPath(candidate)
    if value.is_absolute() or PureWindowsPath(candidate).is_absolute():
        return None
    parts = tuple(value.parts)
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    for part in parts:
        if part.endswith((".", " ")) or ":" in part:
            return None
        stem = part.rsplit(".", 1)[0]
        if stem.upper() in _DOS_RESERVED_NAMES or _SHORT_NAME_PATTERN.match(part):
            return None
    return parts


def _name_words(name: str) -> tuple[str, ...]:
    value = _CAMEL_BOUNDARY_2.sub(" ", name)
    value = _CAMEL_BOUNDARY_1.sub(" ", value)
    value = re.sub(r"[^A-Za-z0-9]+", " ", value)
    return tuple(word.casefold() for word in value.split() if word)


def _sensitive_basename(name: str) -> bool:
    raw_name = name
    name = name.casefold()
    if (
        name == ".env"
        or name.startswith(".env.")
        or name.startswith(".envrc.")
        or name in _SECRET_BASENAMES
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    ):
        return True
    stem = raw_name.rsplit(".", 1)[0]
    words = _name_words(stem)
    return bool(
        set(words) & _SENSITIVE_NAME_WORDS
        or any(
            words[index : index + len(sequence)] == sequence
            for sequence in _SENSITIVE_NAME_SEQUENCES
            for index in range(len(words) - len(sequence) + 1)
        )
    )


def _sensitive_directory(parts: tuple[str, ...]) -> bool:
    lowered = tuple(part.casefold() for part in parts)
    if any(part in _SECRET_COMPONENTS or part in _FORBIDDEN_COMPONENTS for part in lowered):
        return True
    if any(
        lowered[index : index + len(prefix)] == prefix
        for prefix in _SECRET_PATH_PREFIXES
        for index in range(len(parts) - len(prefix) + 1)
    ):
        return True
    for component in parts[:-1]:
        words = _name_words(component)
        if words and words[-1] in _SENSITIVE_DIRECTORY_TERMINALS:
            return True
    return False


def is_restricted_relative_path(candidate: str) -> bool:
    """Return whether a path is invalid, sensitive, or controls the Agent runtime."""

    parts = relative_parts(candidate)
    if parts is None:
        return True
    return (
        _sensitive_directory(parts)
        or parts[-1].casefold() in _FORBIDDEN_BASENAMES
        or _sensitive_basename(parts[-1])
    )


def is_sensitive_relative_path(candidate: str) -> bool:
    """Compatibility name for workspace policy; restricted paths are denied."""

    return is_restricted_relative_path(candidate)


__all__ = [
    "_FORBIDDEN_BASENAMES",
    "_FORBIDDEN_COMPONENTS",
    "_SECRET_BASENAMES",
    "_SECRET_COMPONENTS",
    "_SECRET_PATH_PREFIXES",
    "is_restricted_relative_path",
    "is_sensitive_relative_path",
    "relative_parts",
]
