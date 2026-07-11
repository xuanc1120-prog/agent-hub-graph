"""Frozen v1 primitive types, enums, and the strict base model.

This module is the root of the Agent Hub contract. Every protocol model
inherits :class:`StrictModel` (``extra="forbid"``). Identifiers, hashes and
Git object ids use the constrained aliases defined here; user-visible text
uses the length-limited text aliases; repo paths use :data:`RepoRelativePath`
whose *shape* is bounded here while semantic path validation lives in the
security ``PathPolicy`` (owned by the runtime).

Contract version: see ``docs/contracts/CONTRACT_VERSION.md``.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1"

# --- Constrained scalar aliases -------------------------------------------------

EntityId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GitObjectId = Annotated[str, Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")]

# Repo-relative path shape only. Escape / symlink / reserved-name rejection is
# enforced by the runtime PathPolicy, never by this regex.
RepoRelativePath = Annotated[str, Field(min_length=1, max_length=1_024)]

# Single argv token inside an allowed command template.
ArgvToken = Annotated[str, Field(min_length=1, max_length=4_096)]

# A short enum-like operand used in an if-node condition value.
IfValueToken = Annotated[str, Field(min_length=1, max_length=256)]

# A bounded set of operands used by the ``in`` condition operator. Keeping the
# list constraint on this alias avoids applying ``max_length`` to scalar/bool
# members of the IfCondition value union.
IfValueList = Annotated[list[IfValueToken], Field(min_length=1, max_length=50)]

# A whitelisted command template: a bounded argv vector (e.g. ["pytest", "-q"]).
# Both the token length (via ArgvToken) and the token count are constrained so
# neither the vector nor a single token can grow without bound. Must have at
# least one token (the executable). The outer list of templates is capped at the
# field level where the template list is declared.
CommandTemplate = Annotated[list[ArgvToken], Field(min_length=1, max_length=64)]

# --- User-visible text aliases (length-bounded) --------------------------------

TitleText = Annotated[str, Field(min_length=1, max_length=500)]
InstructionText = Annotated[str, Field(min_length=1, max_length=20_000)]
GoalText = Annotated[str, Field(min_length=1, max_length=20_000)]
SummaryText = Annotated[str, Field(max_length=8_000)]
ReasonText = Annotated[str, Field(max_length=4_000)]
ShortReasonText = Annotated[str, Field(max_length=1_000)]
DescriptionText = Annotated[str, Field(max_length=4_000)]


# --- Strict base ----------------------------------------------------------------


class StrictModel(BaseModel):
    """Base for every protocol model. Rejects unknown fields, validates on
    assignment, and forbids non-finite floats."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        allow_inf_nan=False,
    )


class FrozenStrictModel(StrictModel):
    """Strict base for immutable record/audit models.

    Any model that enforces a cross-field invariant with a
    ``model_validator(mode="after")`` must inherit from this base rather than
    :class:`StrictModel`. Pydantic runs after-validators *after* mutating the
    target field on assignment, so with ``validate_assignment`` a single-field
    assignment that violates the invariant would raise yet still leave the
    object mutated and illegal. ``frozen=True`` blocks assignment entirely, so
    the invariant established at construction can never be broken in place;
    state transitions reconstruct a new instance through the validating
    constructor. See ADR 0001.
    """

    model_config = ConfigDict(frozen=True)


def canonical_json(model: BaseModel) -> bytes:
    """Deterministic UTF-8 serialization used for subject/state hashes.

    Keys are sorted and separators are compact so the same logical contract
    value always produces identical bytes regardless of field declaration
    order. Enums serialize by value and datetimes by ISO string.
    """

    payload: Any = model.model_dump(mode="json")
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


# --- Shared enums ---------------------------------------------------------------


class RiskLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class PlannerType(StrEnum):
    RULE_BASED = "rule_based"
    OPEN_CODE = "open_code"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"
    ARCHIVED = "archived"


class PlannerRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PRIVILEGE_REQUESTED = "privilege_requested"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    BLOCKED_BY_GUARD = "blocked_by_guard"
    PARSE_FAILED = "parse_failed"
    ORPHANED = "orphaned"


class SecuritySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


class ActorType(StrEnum):
    USER = "user"
    MASTER = "master"
    AGENT = "agent"
    SYSTEM = "system"
    LOCAL_CLI = "local_cli"


class ArtifactType(StrEnum):
    LOG = "log"
    CONSOLE = "console"
    DIFF = "diff"
    PATCH = "patch"
    REPORT = "report"
    TEST_RESULT = "test_result"
    RUNTIME_POLICY = "runtime_policy"
    CHANGE_PREIMAGE = "change_preimage"


class ConsoleStreamKind(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"
    SYSTEM = "system"


class ConsoleOwnerType(StrEnum):
    TASK = "task"
    PLANNER_RUN = "planner_run"


class CapabilityType(StrEnum):
    MODIFY_DEPENDENCY = "modify_dependency"
    MODIFY_CONFIG = "modify_config"


class PrivilegeAction(StrEnum):
    EDIT_DEPENDENCY_MANIFEST = "edit_dependency_manifest"
    EDIT_PROJECT_CONFIG = "edit_project_config"


class NodeType(StrEnum):
    INPUT = "input"
    AGENT_TASK = "agent_task"
    CONTEXT_BUILDER = "context_builder"
    PATCH_GUARD = "patch_guard"
    COMMAND_GUARD = "command_guard"
    TEST = "test"
    RISK_CLASSIFIER = "risk_classifier"
    APPROVAL = "approval"
    MERGE_PATCH = "merge_patch"
    OUTPUT = "output"
    IF = "if"


class TaskKind(StrEnum):
    ANALYZE = "analyze"
    IMPLEMENT = "implement"
    REVIEW = "review"
    DOCS = "docs"
    TEST_FIX = "test_fix"


class TestKind(StrEnum):
    COMMAND = "command"
    DOCS_STATIC = "docs_static"


class IfOperator(StrEnum):
    EQ = "eq"
    NE = "ne"
    IN = "in"
    IS_TRUE = "is_true"
    IS_FALSE = "is_false"


class AssignmentMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"
    LOCKED = "locked"


class NodeRunStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    BLOCKED_BY_GUARD = "blocked_by_guard"
    FAILED = "failed"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"


class WorkflowRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"


class NodeOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    MATCHED = "matched"
    NOT_MATCHED = "not_matched"
    APPROVED = "approved"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class EdgeCondition(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    MATCHED = "matched"
    NOT_MATCHED = "not_matched"
    APPROVED = "approved"
    REJECTED = "rejected"


class ArtifactRef(FrozenStrictModel):
    """Content-addressed pointer to an artifact stored outside the DB.

    Frozen: it is a content-addressed value object (its ``sha256``/``size_bytes``
    identify fixed bytes) and it is embedded in frozen records like
    :class:`~protocol.console.ConsoleChunk` whose invariants read these fields.
    Freezing it closes the nested-mutation hole where a parent record is frozen
    but a mutable child could still break the parent's cross-field invariant.
    """

    artifact_id: EntityId
    artifact_type: ArtifactType
    relative_path: RepoRelativePath
    sha256: Sha256Hex
    size_bytes: int = Field(ge=0)
