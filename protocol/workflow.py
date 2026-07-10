"""Frozen v1 workflow graph contracts.

One :class:`WorkflowNode` / :class:`WorkflowEdge` shape is shared by the
author-facing and compiled graphs, but :class:`AuthorGraph` and
:class:`CompiledGraph` are deliberately distinct wrappers: users and the
Planner edit an ``AuthorGraph``; only the deterministic ``WorkflowCompiler``
constructs a ``CompiledGraph``. Compiler-only fields (``effective_*``,
``policy_risk_floor``, ``requires_changeset_approval``, ``test_kind``,
``test_argv``) exist on the shared node so compiled nodes can carry them;
DraftValidator rejects them on author/Planner/API input.

Run state lives only in ``node_runs`` / ``workflow_runs``; canvas positions
live only in :class:`WorkflowLayout`. Neither appears on ``WorkflowNode``.
"""

from __future__ import annotations

from pydantic import Field

from protocol.common import (
    CONTRACT_VERSION,
    ArgvToken,
    AssignmentMode,
    DescriptionText,
    EdgeCondition,
    EntityId,
    GitObjectId,
    GoalText,
    IfOperator,
    InstructionText,
    NodeType,
    PlannerType,
    RepoRelativePath,
    RiskLevel,
    Sha256Hex,
    ShortReasonText,
    StrictModel,
    TaskKind,
    TestKind,
    TitleText,
)

CommandTemplate = list[ArgvToken]


class NodePosition(StrictModel):
    x: float = Field(ge=-1_000_000, le=1_000_000)
    y: float = Field(ge=-1_000_000, le=1_000_000)


class NodeLayout(StrictModel):
    node_id: EntityId
    position: NodePosition


class WorkflowLayout(StrictModel):
    """AuthorGraph canvas coordinates only. CompiledGraph system-node
    positions are never persisted and never hashed."""

    nodes: list[NodeLayout] = Field(default_factory=list, max_length=100)


class AgentRecommendation(StrictModel):
    agent_id: EntityId
    score: int = Field(ge=0, le=100)
    reason: ShortReasonText


class IfCondition(StrictModel):
    """Structured branch predicate for an ``if`` node. Only whitelisted
    upstream fields and a limited operator set are permitted; script or
    natural-language conditions are rejected by construction."""

    upstream_node_id: EntityId
    field: str = Field(pattern=r"^(status|outcome|effective_risk|tests_passed)$")
    operator: IfOperator
    value: str | bool | list[str] | None = None


class WorkflowNode(StrictModel):
    """Node definition shared by AuthorGraph and CompiledGraph.

    Carries no run status and no canvas position. ``candidate`` fields are
    author-editable proposals; ``effective_*`` / ``policy_risk_floor`` /
    ``requires_changeset_approval`` / ``test_kind`` / ``test_argv`` are
    compiler-only and are rejected by DraftValidator on author input.
    """

    id: EntityId
    node_type: NodeType
    task_kind: TaskKind | None = None
    title: TitleText
    description: DescriptionText | None = None
    instruction: InstructionText | None = None
    assigned_agent: EntityId | None = None
    assignment_mode: AssignmentMode = AssignmentMode.AUTO
    resolved_agent_id: EntityId | None = None
    resolved_agent_spec_sha256: Sha256Hex | None = None
    recommended_agents: list[AgentRecommendation] = Field(default_factory=list, max_length=50)
    allowed_files_candidate: list[RepoRelativePath] = Field(default_factory=list, max_length=500)
    new_files_candidate: list[RepoRelativePath] = Field(default_factory=list, max_length=100)
    allowed_commands_candidate: list[CommandTemplate] = Field(default_factory=list, max_length=50)
    effective_allowed_files: list[RepoRelativePath] | None = Field(default=None, max_length=100)
    effective_new_files: list[RepoRelativePath] | None = Field(default=None, max_length=100)
    effective_allowed_commands: list[CommandTemplate] | None = Field(default=None, max_length=20)
    policy_risk_floor: RiskLevel | None = None
    requires_changeset_approval: bool | None = None
    test_kind: TestKind | None = None
    test_argv: list[ArgvToken] | None = Field(default=None, max_length=64)
    if_condition: IfCondition | None = None
    risk_level_hint: RiskLevel = RiskLevel.L1
    requires_write: bool = False
    system_managed: bool = False
    source_node_id: EntityId | None = None
    system_rule_id: EntityId | None = None


class WorkflowEdge(StrictModel):
    id: EntityId
    from_node: EntityId
    to_node: EntityId
    condition: EdgeCondition = EdgeCondition.SUCCESS
    system_managed: bool = False


class WorkflowDraft(StrictModel):
    """The only structured output a Planner may emit. The execution layer
    never runs a natural-language plan; a draft is converted explicitly into
    an :class:`AuthorGraph`."""

    schema_version: str = CONTRACT_VERSION
    session_id: EntityId
    goal: GoalText
    planner_id: EntityId
    planner_type: PlannerType
    planner_model: str | None = Field(default=None, max_length=200)
    nodes: list[WorkflowNode] = Field(default_factory=list, max_length=100)
    edges: list[WorkflowEdge] = Field(default_factory=list, max_length=300)
    assumptions: list[ShortReasonText] = Field(default_factory=list, max_length=100)
    risks: list[ShortReasonText] = Field(default_factory=list, max_length=100)
    required_user_inputs: list[ShortReasonText] = Field(default_factory=list, max_length=50)


class AuthorGraph(StrictModel):
    """User/Planner-editable graph. May be persisted while structurally
    incomplete; it cannot be executed directly."""

    schema_version: str = CONTRACT_VERSION
    nodes: list[WorkflowNode] = Field(default_factory=list, max_length=100)
    edges: list[WorkflowEdge] = Field(default_factory=list, max_length=300)


class CompiledGraph(StrictModel):
    """Deterministic executable graph. Constructed only by the
    ``WorkflowCompiler``; the API never accepts a client-submitted
    ``CompiledGraph``. Larger node/edge ceilings account for injected system
    security nodes."""

    schema_version: str = CONTRACT_VERSION
    source_author_hash: Sha256Hex
    integration_base_commit: GitObjectId
    policy_version: str = Field(min_length=1, max_length=64)
    agent_catalog_snapshot_hash: Sha256Hex
    nodes: list[WorkflowNode] = Field(default_factory=list, max_length=300)
    edges: list[WorkflowEdge] = Field(default_factory=list, max_length=600)
