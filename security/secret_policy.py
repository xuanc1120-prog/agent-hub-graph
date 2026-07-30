"""Shared secret detection and redaction for persisted runtime data."""

from __future__ import annotations

import re

_MAX_SCANNABLE_BYTES = 8 * 1024 * 1024

_SECRET_PATTERNS = (
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
    re.compile(
        r"(?i)\b(?:"
        r"api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
        r"password|passwd|private[_-]?key|secret|"
        r"database[_-]?url|db[_-]?url|sqlalchemy\.url"
        r")\b\s*[=:]\s*"
        r"(?:[\"'][^\"'\r\n]{1,4096}[\"']|[^\s,;]{1,4096})"
    ),
)


class SecretPolicyViolation(ValueError):
    """Content cannot safely enter an artifact or runtime output."""


def redact_secret_text(value: str) -> str:
    """Replace every recognized credential form without exposing its value."""

    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


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
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise SecretPolicyViolation(f"{label} contains credential-like content")


__all__ = [
    "SecretPolicyViolation",
    "assert_secret_free_bytes",
    "redact_secret_text",
]
