"""Shared secret detection and redaction for persisted runtime data."""

from __future__ import annotations

import re

_MAX_SCANNABLE_BYTES = 8 * 1024 * 1024

_FIXED_SECRET_PATTERNS = (
    re.compile(
        r"-----BEGIN [^-\r\n]{0,64}PRIVATE KEY-----[\s\S]*?"
        r"(?:-----END [^-\r\n]{0,64}PRIVATE KEY-----|\Z)",
        re.I,
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,255}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b"),
    re.compile(
        r"(?i)\b[a-z][a-z0-9+._-]{0,63}://"
        r"[^\s/?#@]{1,1024}@[^\s<>'\"`,;]{1,2048}"
    ),
)
_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)(?<![a-z0-9_])"
    r"(?:export[ \t]+)?"
    r"(?P<quote>[\"']?)"
    r"(?P<key>[a-z][a-z0-9_.-]{0,127})"
    r"(?P=quote)[ \t]*[=:][ \t]*"
    r"(?:[\"'][^\"'\r\n]{1,4096}[\"']|[^\s,;]{1,4096})"
)
_XML_ELEMENT_PATTERN = re.compile(
    r"(?is)<(?P<tag>[a-z_][a-z0-9_.:-]{0,127})\b[^>]{0,1024}>"
    r"[^<]{1,4096}"
    r"</(?P=tag)\s*>"
)
_CAMEL_BOUNDARY_1 = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_BOUNDARY_2 = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_SENSITIVE_WORDS = frozenset(
    {
        "credential",
        "credentials",
        "pass",
        "passwd",
        "password",
        "pwd",
        "secret",
        "token",
    }
)
_SENSITIVE_SEQUENCES = frozenset(
    {
        ("access", "key"),
        ("api", "key"),
        ("auth", "key"),
        ("database", "url"),
        ("db", "url"),
        ("private", "key"),
        ("sqlalchemy", "url"),
    }
)


class SecretPolicyViolation(ValueError):
    """Content cannot safely enter an artifact or runtime output."""


def redact_secret_text(value: str) -> str:
    """Replace every recognized credential form without exposing its value."""

    for pattern in _FIXED_SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    value = _ASSIGNMENT_PATTERN.sub(_redact_sensitive_assignment, value)
    return _XML_ELEMENT_PATTERN.sub(_redact_sensitive_xml_element, value)


def _redact_sensitive_assignment(match: re.Match[str]) -> str:
    return "[REDACTED]" if _is_sensitive_key(match.group("key")) else match.group(0)


def _redact_sensitive_xml_element(match: re.Match[str]) -> str:
    return "[REDACTED]" if _is_sensitive_key(match.group("tag")) else match.group(0)


def _is_sensitive_key(key: str) -> bool:
    local_name = key.rsplit(":", 1)[-1]
    expanded = _CAMEL_BOUNDARY_2.sub(" ", _CAMEL_BOUNDARY_1.sub(" ", local_name))
    words = tuple(word.casefold() for word in re.findall(r"[A-Za-z0-9]+", expanded))
    if not words:
        return False
    if any(word in _SENSITIVE_WORDS for word in words):
        return True
    for size in (2, 3):
        if any(
            tuple(words[index : index + size]) in _SENSITIVE_SEQUENCES
            for index in range(len(words) - size + 1)
        ):
            return True
    return False


def _contains_structured_secret(value: str) -> bool:
    return any(
        _is_sensitive_key(match.group("key")) for match in _ASSIGNMENT_PATTERN.finditer(value)
    ) or any(
        _is_sensitive_key(match.group("tag")) for match in _XML_ELEMENT_PATTERN.finditer(value)
    )


def assert_secret_free_bytes(content: bytes, *, label: str) -> None:
    """Fail closed when ignored workspace content cannot be classified as safe."""

    if len(content) > _MAX_SCANNABLE_BYTES:
        raise SecretPolicyViolation(f"{label} exceeds the secret scan limit")
    if b"\x00" in content:
        raise SecretPolicyViolation(f"{label} is not safely classifiable text")
    try:
        value = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise SecretPolicyViolation(f"{label} is not valid UTF-8 text") from error
    if any(
        pattern.search(value) for pattern in _FIXED_SECRET_PATTERNS
    ) or _contains_structured_secret(value):
        raise SecretPolicyViolation(f"{label} contains credential-like content")


__all__ = [
    "SecretPolicyViolation",
    "assert_secret_free_bytes",
    "redact_secret_text",
]
