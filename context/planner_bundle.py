"""Bounded, redacted context passed to workflow planners."""

from __future__ import annotations

import re
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from protocol import (
    EntityId,
    FrozenStrictModel,
    GitObjectId,
    GoalText,
    Sha256Hex,
    canonical_json,
)

MAX_PLANNER_FILES = 5_000
MAX_PLANNER_BYTES = 20 * 1024 * 1024

PlannerHint = Annotated[
    str,
    Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._+#/()-]*$",
    ),
]
PlannerHints = Annotated[tuple[PlannerHint, ...], Field(max_length=50)]

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:sk[-_]|ghp_)[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[^\s]{8,}",
        re.IGNORECASE,
    ),
)


def _reject_secret_like_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("planner context text cannot contain NUL bytes")
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise ValueError("planner context contains secret-like text; redact it first")
    return value


def _normalize_hints(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: dict[str, str] = {}
    for value in values:
        cleaned = _reject_secret_like_text(value.strip())
        key = cleaned.casefold()
        current = normalized.get(key)
        if current is None or cleaned < current:
            normalized[key] = cleaned
    return tuple(normalized[key] for key in sorted(normalized))


class _PlannerContextPayload(FrozenStrictModel):
    """Hashable planner metadata; intentionally carries no paths or file bodies."""

    session_id: EntityId
    goal: GoalText
    integration_base_commit: GitObjectId
    file_count: int = Field(default=0, ge=0, le=MAX_PLANNER_FILES)
    total_size_bytes: int = Field(default=0, ge=0, le=MAX_PLANNER_BYTES)
    languages: PlannerHints = ()
    test_frameworks: PlannerHints = ()
    truncated: bool = False
    redaction_applied: Literal[True] = True

    @field_validator("goal")
    @classmethod
    def reject_secrets_in_goal(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("planner goal cannot be blank")
        return _reject_secret_like_text(normalized)

    @field_validator("languages", "test_frameworks")
    @classmethod
    def normalize_and_check_hints(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_hints(value)


class PlannerContextBundle(_PlannerContextPayload):
    """Immutable planner input snapshot tied to one integration commit.

    The model deliberately omits source/shared repository paths, environment
    variables, arbitrary dictionaries, and file content. A later context
    builder may materialize a separate read-only planner view, but planners
    still receive only this bounded manifest metadata and its verified hash.
    """

    bundle_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_bundle_hash(self) -> Self:
        expected = _payload_hash(self)
        if self.bundle_hash != expected:
            raise ValueError("bundle_hash does not match planner context payload")
        return self

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        goal: str,
        integration_base_commit: str,
        file_count: int = 0,
        total_size_bytes: int = 0,
        languages: tuple[str, ...] = (),
        test_frameworks: tuple[str, ...] = (),
        truncated: bool = False,
    ) -> Self:
        payload = _PlannerContextPayload(
            session_id=session_id,
            goal=goal,
            integration_base_commit=integration_base_commit,
            file_count=file_count,
            total_size_bytes=total_size_bytes,
            languages=languages,
            test_frameworks=test_frameworks,
            truncated=truncated,
            redaction_applied=True,
        )
        return cls(**payload.model_dump(mode="python"), bundle_hash=_payload_hash(payload))


def _payload_hash(bundle: _PlannerContextPayload) -> Sha256Hex:
    payload = _PlannerContextPayload.model_validate(
        bundle.model_dump(mode="python", exclude={"bundle_hash"})
    )
    return Sha256Hex(sha256(canonical_json(payload)).hexdigest())


def compute_bundle_hash(bundle: PlannerContextBundle) -> Sha256Hex:
    """Recompute the canonical hash without trusting ``bundle_hash``."""

    return _payload_hash(bundle)


async def build_planner_context(
    *,
    session_id: str,
    goal: str,
    integration_base_commit: str,
    file_count: int = 0,
    total_size_bytes: int = 0,
    languages: tuple[str, ...] = (),
    test_frameworks: tuple[str, ...] = (),
    truncated: bool = False,
) -> PlannerContextBundle:
    """Build the bounded metadata portion of a planner context snapshot.

    Repository export, PathPolicy filtering, ACLs, and artifact persistence are
    later runtime responsibilities. This stage cannot accept a repository path,
    which prevents a planner from receiving unrestricted source access by API
    accident.
    """

    return PlannerContextBundle.create(
        session_id=session_id,
        goal=goal,
        integration_base_commit=integration_base_commit,
        file_count=file_count,
        total_size_bytes=total_size_bytes,
        languages=languages,
        test_frameworks=test_frameworks,
        truncated=truncated,
    )
