"""Shared secret detection and redaction for persisted runtime data."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from xml.etree import ElementTree

_MAX_SCANNABLE_BYTES = 8 * 1024 * 1024
_MAX_STRUCTURED_NODES = 100_000

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
_QUOTED_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)(?<![a-z0-9_])"
    r"(?:export[ \t]+)?"
    r"(?P<key>\"(?:\\.|[^\"\\\r\n]){1,512}\"|'[^'\r\n]{1,512}')"
    r"[ \t]*[=:][ \t]*"
    r"(?:\"(?:\\.|[^\"\\\r\n]){1,4096}\"|'[^'\r\n]{1,4096}'|[^\s,;]{1,4096})"
)
_QUOTED_KEY_SEPARATOR_PATTERN = re.compile(
    r"(?is)(?<![a-z0-9_])"
    r"(?P<key>\"(?:\\.|[^\"\\\r\n]){1,512}\"|'[^'\r\n]{1,512}')"
    r"\s*[:=]"
)
_TOML_KEY_SEPARATOR_PATTERN = re.compile(
    r"(?im)(?<![a-z0-9_])"
    r"(?P<key>[a-z0-9_-]{1,128}(?:[ \t]*\.[ \t]*[a-z0-9_-]{1,128})*)"
    r"[ \t]*="
)
_XML_ATTRIBUTE_KEY_PATTERN = re.compile(
    r"(?is)(?<![a-z0-9_.:-])"
    r"(?P<key>[a-z_][a-z0-9_.:-]{0,127})"
    r"\s*=\s*(?P<quote>[\"'])"
)
_TOML_TABLE_KEY_PATTERN = re.compile(r"(?im)^\s*(?P<key>\[\[?[^\]\r\n]{1,512}\]\]?)")
_XML_ELEMENT_KEY_PATTERN = re.compile(r"(?is)<(?P<key>[a-z_][a-z0-9_.:-]{0,127})\b")
_XML_ELEMENT_PATTERN = re.compile(
    r"(?is)<(?P<tag>[a-z_][a-z0-9_.:-]{0,127})\b[^>]{0,1024}>"
    r"[^<]{1,4096}"
    r"</(?P=tag)\s*>"
)
_XML_CDATA_PATTERN = re.compile(
    r"(?is)<(?P<tag>[a-z_][a-z0-9_.:-]{0,127})\b[^>]{0,1024}>"
    r"\s*<!\[CDATA\[[\s\S]{1,4096}?\]\]>\s*"
    r"</(?P=tag)\s*>"
)
_XML_ATTRIBUTE_PATTERN = re.compile(
    r"(?is)(?P<key>[a-z_][a-z0-9_.:-]{0,127})\s*=\s*"
    r"(?:\"[^\"\r\n]{1,4096}\"|'[^'\r\n]{1,4096}')"
)
_TOML_DOCUMENT_HINT = re.compile(
    r"(?m)^\s*(?:\[\[?[^\]\r\n]{1,512}\]\]?|"
    r"(?:\"(?:\\.|[^\"\\\r\n]){1,512}\"|'[^'\r\n]{1,512}')\s*=|"
    r"[A-Za-z0-9_-]{1,128}(?:\s*\.\s*[A-Za-z0-9_-]{1,128})*\s*=[^\r\n]*(?:\"\"\"|'''))"
)
_XML_UNSAFE_DECLARATION = re.compile(r"(?is)<!\s*(?:DOCTYPE|ENTITY)\b")
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

    if _contains_ambiguous_sensitive_key(value):
        return "[REDACTED]"
    structured = _redact_structured_document(value)
    for pattern in _FIXED_SECRET_PATTERNS:
        structured = pattern.sub("[REDACTED]", structured)
    if structured != value:
        return structured
    value = structured
    value = _QUOTED_ASSIGNMENT_PATTERN.sub(
        _redact_sensitive_quoted_assignment,
        value,
    )
    value = _ASSIGNMENT_PATTERN.sub(_redact_sensitive_assignment, value)
    value = _XML_CDATA_PATTERN.sub(_redact_sensitive_xml_element, value)
    value = _XML_ELEMENT_PATTERN.sub(_redact_sensitive_xml_element, value)
    return _XML_ATTRIBUTE_PATTERN.sub(_redact_sensitive_xml_attribute, value)


def _contains_ambiguous_sensitive_key(value: str) -> bool:
    """Fail closed when mixed console text contains an unparseable secret key."""

    if _is_complete_structured_document(value):
        return False
    if any(
        _is_sensitive_key(decoded_key)
        for match in _QUOTED_KEY_SEPARATOR_PATTERN.finditer(value)
        if (decoded_key := _decode_quoted_key(match.group("key"))) is not None
    ):
        return True
    return any(
        _is_sensitive_key(match.group("key"))
        for pattern in (
            _TOML_KEY_SEPARATOR_PATTERN,
            _XML_ATTRIBUTE_KEY_PATTERN,
            _TOML_TABLE_KEY_PATTERN,
            _XML_ELEMENT_KEY_PATTERN,
        )
        for match in pattern.finditer(value)
    )


def _is_complete_structured_document(value: str) -> bool:
    stripped = value.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            json.loads(value)
        except (json.JSONDecodeError, RecursionError):
            if not _TOML_DOCUMENT_HINT.search(value):
                return False
            try:
                tomllib.loads(value)
            except (tomllib.TOMLDecodeError, RecursionError):
                return False
        return True
    if stripped.startswith("<"):
        if _XML_UNSAFE_DECLARATION.search(value):
            return False
        try:
            ElementTree.fromstring(value)
        except (ElementTree.ParseError, RecursionError):
            return False
        return True
    if not _TOML_DOCUMENT_HINT.search(value):
        return False
    try:
        tomllib.loads(value)
    except (tomllib.TOMLDecodeError, RecursionError):
        return False
    return True


def _redact_structured_document(value: str) -> str:
    """Redact parsed structured documents, failing closed when parsing is unsafe."""

    stripped = value.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            document = json.loads(value)
        except (json.JSONDecodeError, RecursionError):
            if _TOML_DOCUMENT_HINT.search(value):
                return _redact_toml_document(value)
            return "[REDACTED]" if stripped.endswith(("}", "]")) else value
        try:
            redacted, changed = _redact_json_value(document, budget=[0])
        except SecretPolicyViolation:
            return "[REDACTED]"
        if not changed:
            return value
        return json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))

    if stripped.startswith("<"):
        if _XML_UNSAFE_DECLARATION.search(value):
            return "[REDACTED]"
        try:
            root = ElementTree.fromstring(value)
        except (ElementTree.ParseError, RecursionError):
            return "[REDACTED]"
        changed = False
        for element in list(root.iter()):
            if isinstance(element.tag, str) and _is_sensitive_key(element.tag):
                element.clear()
                element.text = "[REDACTED]"
                changed = True
                continue
            for key in list(element.attrib):
                if _is_sensitive_key(key):
                    element.attrib[key] = "[REDACTED]"
                    changed = True
        return ElementTree.tostring(root, encoding="unicode") if changed else value

    if _TOML_DOCUMENT_HINT.search(value):
        return _redact_toml_document(value)
    return value


def _redact_toml_document(value: str) -> str:
    try:
        document = tomllib.loads(value)
    except (tomllib.TOMLDecodeError, RecursionError):
        return "[REDACTED]"
    try:
        if _mapping_contains_sensitive_key(document, budget=[0]):
            return "[REDACTED]"
    except SecretPolicyViolation:
        return "[REDACTED]"
    return value


def _redact_json_value(value: object, *, budget: list[int]) -> tuple[object, bool]:
    budget[0] += 1
    if budget[0] > _MAX_STRUCTURED_NODES:
        raise SecretPolicyViolation("structured redaction exceeds the scan limit")
    if isinstance(value, Mapping):
        redacted: dict[object, object] = {}
        changed = False
        for key, child in value.items():
            if isinstance(key, str) and _is_sensitive_key(key):
                redacted[key] = "[REDACTED]"
                changed = True
                continue
            replacement, child_changed = _redact_json_value(child, budget=budget)
            redacted[key] = replacement
            changed |= child_changed
        return redacted, changed
    if isinstance(value, list):
        redacted_items: list[object] = []
        changed = False
        for child in value:
            replacement, child_changed = _redact_json_value(child, budget=budget)
            redacted_items.append(replacement)
            changed |= child_changed
        return redacted_items, changed
    return value, False


def _mapping_contains_sensitive_key(document: object, *, budget: list[int]) -> bool:
    pending = [document]
    while pending:
        current = pending.pop()
        budget[0] += 1
        if budget[0] > _MAX_STRUCTURED_NODES:
            raise SecretPolicyViolation("structured redaction exceeds the scan limit")
        if isinstance(current, Mapping):
            for key, child in current.items():
                if isinstance(key, str) and _is_sensitive_key(key):
                    return True
                pending.append(child)
        elif isinstance(current, Sequence) and not isinstance(
            current,
            (str, bytes, bytearray),
        ):
            pending.extend(current)
    return False


def _redact_sensitive_quoted_assignment(match: re.Match[str]) -> str:
    decoded_key = _decode_quoted_key(match.group("key"))
    if decoded_key is None or _is_sensitive_key(decoded_key):
        return "[REDACTED]"
    return match.group(0)


def _redact_sensitive_assignment(match: re.Match[str]) -> str:
    return "[REDACTED]" if _is_sensitive_key(match.group("key")) else match.group(0)


def _redact_sensitive_xml_element(match: re.Match[str]) -> str:
    return "[REDACTED]" if _is_sensitive_key(match.group("tag")) else match.group(0)


def _redact_sensitive_xml_attribute(match: re.Match[str]) -> str:
    return "[REDACTED]" if _is_sensitive_key(match.group("key")) else match.group(0)


def _decode_quoted_key(key: str) -> str | None:
    if key.startswith('"'):
        try:
            decoded = json.loads(key)
        except (json.JSONDecodeError, TypeError):
            return None
        return decoded if isinstance(decoded, str) else None
    if key.startswith("'") and key.endswith("'"):
        return key[1:-1]
    return None


def _is_sensitive_key(key: str) -> bool:
    local_name = key.rsplit("}", 1)[-1].rsplit(":", 1)[-1]
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
    for match in _QUOTED_ASSIGNMENT_PATTERN.finditer(value):
        decoded_key = _decode_quoted_key(match.group("key"))
        if decoded_key is None or _is_sensitive_key(decoded_key):
            return True
    key_patterns = (
        (_ASSIGNMENT_PATTERN, "key"),
        (_XML_ATTRIBUTE_PATTERN, "key"),
        (_XML_CDATA_PATTERN, "tag"),
        (_XML_ELEMENT_PATTERN, "tag"),
    )
    return any(
        _is_sensitive_key(match.group(group_name))
        for pattern, group_name in key_patterns
        for match in pattern.finditer(value)
    )


def _assert_parsed_document_secret_free(value: str, *, label: str) -> None:
    stripped = value.lstrip()
    if not stripped:
        return
    if stripped.startswith("{"):
        try:
            document = json.loads(value)
        except json.JSONDecodeError as error:
            raise SecretPolicyViolation(f"{label} is malformed JSON") from error
        _assert_no_sensitive_mapping_keys(document, label=label)
        return
    if stripped.startswith("["):
        try:
            document = json.loads(value)
        except json.JSONDecodeError as json_error:
            if not _TOML_DOCUMENT_HINT.search(value):
                raise SecretPolicyViolation(f"{label} is malformed JSON") from json_error
            _assert_toml_document_secret_free(value, label=label)
        else:
            _assert_no_sensitive_mapping_keys(document, label=label)
        return
    if stripped.startswith("<"):
        if _XML_UNSAFE_DECLARATION.search(value):
            raise SecretPolicyViolation(f"{label} contains an unsafe XML declaration")
        try:
            root = ElementTree.fromstring(value)
        except ElementTree.ParseError as error:
            raise SecretPolicyViolation(f"{label} is malformed XML") from error
        _assert_no_sensitive_xml_keys(root, label=label)
        return
    if _TOML_DOCUMENT_HINT.search(value):
        _assert_toml_document_secret_free(value, label=label)


def _assert_toml_document_secret_free(value: str, *, label: str) -> None:
    try:
        document = tomllib.loads(value)
    except tomllib.TOMLDecodeError as error:
        raise SecretPolicyViolation(f"{label} is malformed TOML") from error
    _assert_no_sensitive_mapping_keys(document, label=label)


def _assert_no_sensitive_mapping_keys(document: object, *, label: str) -> None:
    pending = [document]
    visited = 0
    while pending:
        current = pending.pop()
        visited += 1
        if visited > _MAX_STRUCTURED_NODES:
            raise SecretPolicyViolation(f"{label} exceeds the structured scan limit")
        if isinstance(current, Mapping):
            for key, child in current.items():
                if isinstance(key, str) and _is_sensitive_key(key):
                    raise SecretPolicyViolation(f"{label} contains credential-like content")
                pending.append(child)
        elif isinstance(current, Sequence) and not isinstance(
            current,
            (str, bytes, bytearray),
        ):
            pending.extend(current)


def _assert_no_sensitive_xml_keys(root: ElementTree.Element, *, label: str) -> None:
    for index, element in enumerate(root.iter(), start=1):
        if index > _MAX_STRUCTURED_NODES:
            raise SecretPolicyViolation(f"{label} exceeds the structured scan limit")
        if isinstance(element.tag, str) and _is_sensitive_key(element.tag):
            raise SecretPolicyViolation(f"{label} contains credential-like content")
        if any(_is_sensitive_key(key) for key in element.attrib):
            raise SecretPolicyViolation(f"{label} contains credential-like content")


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
    _assert_parsed_document_secret_free(value, label=label)
    if any(
        pattern.search(value) for pattern in _FIXED_SECRET_PATTERNS
    ) or _contains_structured_secret(value):
        raise SecretPolicyViolation(f"{label} contains credential-like content")


__all__ = [
    "SecretPolicyViolation",
    "assert_secret_free_bytes",
    "redact_secret_text",
]
