"""Contract boundary, validation and serialization tests for HUB-010.

These tests pin the frozen v1 contract: extra=forbid rejection, enum coverage,
constrained ID/hash/GitObjectId aliases, compiler-only fields on the shared
node, cross-field model validators, the Approval discriminated union, and
deterministic canonical serialization.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from protocol import (
    CONTRACT_VERSION,
    ActorType,
    AgentOutputEnvelope,
    AgentResultStatus,
    Approval,
    ApprovalStatus,
    ArtifactRef,
    ArtifactType,
    AuthorGraph,
    CapabilityGrant,
    ChangeSetApproval,
    ChangeSetStatus,
    CompiledGraph,
    ConsoleChunk,
    ContextPack,
    EventEnvelope,
    IfCondition,
    NextSuggestion,
    NodeRunStatus,
    NodeSummary,
    PrivilegeApproval,
    PrivilegeRequestProposal,
    PrivilegeRequestStatus,
    RiskLevel,
    StrictModel,
    TaskStatus,
    WorkflowNode,
    WorkflowRunStatus,
    canonical_json,
)
from protocol.event import Artifact

PLACEHOLDER_SHA = "a" * 64
PLACEHOLDER_COMMIT = "b" * 40


def _artifact_ref(artifact_type: ArtifactType = ArtifactType.DIFF) -> ArtifactRef:
    return ArtifactRef(
        artifact_id="art-1",
        artifact_type=artifact_type,
        relative_path="artifacts/diff-1.patch",
        sha256=PLACEHOLDER_SHA,
        size_bytes=10,
    )


def _minimal_node() -> WorkflowNode:
    return WorkflowNode(id="n1", node_type="agent_task", task_kind="implement", title="Do a thing")


# --- extra=forbid ---------------------------------------------------------------


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowNode(
            id="n1",
            node_type="agent_task",
            task_kind="implement",
            title="t",
            surprise="boom",
        )


def test_strict_base_forbids_extra_and_inf_nan() -> None:
    assert StrictModel.model_config["extra"] == "forbid"
    assert StrictModel.model_config["validate_assignment"] is True
    assert StrictModel.model_config["allow_inf_nan"] is False


# --- enum coverage --------------------------------------------------------------


def test_enum_illegal_value_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowNode(id="n1", node_type="not_a_node_type", title="t")


@pytest.mark.parametrize(
    "enum_cls",
    [
        RiskLevel,
        TaskStatus,
        NodeRunStatus,
        WorkflowRunStatus,
        ChangeSetStatus,
        ApprovalStatus,
        PrivilegeRequestStatus,
        AgentResultStatus,
        ActorType,
    ],
)
def test_status_enums_are_explicit(enum_cls: type) -> None:
    # Each state machine has an explicit, non-empty Enum rather than free strings.
    assert len(list(enum_cls)) >= 3


# --- constrained scalar aliases -------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "",  # empty
        "a" * 129,  # too long
        "../etc/passwd",  # path chars
        "with space",
        "-leading-dash",  # leading dash rejected by first-char class
        "\x00null",  # control char
        ".hidden",  # leading dot rejected by first-char class
    ],
)
def test_entity_id_rejects_bad_values(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        ArtifactRef(
            artifact_id=bad_id,
            artifact_type=ArtifactType.DIFF,
            relative_path="p",
            sha256=PLACEHOLDER_SHA,
            size_bytes=1,
        )


@pytest.mark.parametrize(
    "bad_sha",
    [
        "a" * 63,  # too short
        "a" * 65,  # too long
        "A" * 64,  # uppercase not hex-lower
        "g" * 64,  # non-hex
    ],
)
def test_sha256_alias_rejects_bad_values(bad_sha: str) -> None:
    with pytest.raises(ValidationError):
        ArtifactRef(
            artifact_id="art-1",
            artifact_type=ArtifactType.DIFF,
            relative_path="p",
            sha256=bad_sha,
            size_bytes=1,
        )


@pytest.mark.parametrize(
    "bad_commit",
    ["", "b" * 39, "b" * 41, "b" * 63, "b" * 65, "z" * 40],
)
def test_git_object_id_rejects_bad_values(bad_commit: str) -> None:
    with pytest.raises(ValidationError):
        CompiledGraph(
            source_author_hash=PLACEHOLDER_SHA,
            integration_base_commit=bad_commit,
            policy_version="1",
            agent_catalog_snapshot_hash=PLACEHOLDER_SHA,
        )


def test_git_object_id_accepts_sha1_and_sha256_lengths() -> None:
    for commit in ("b" * 40, "b" * 64):
        graph = CompiledGraph(
            source_author_hash=PLACEHOLDER_SHA,
            integration_base_commit=commit,
            policy_version="1",
            agent_catalog_snapshot_hash=PLACEHOLDER_SHA,
        )
        assert graph.integration_base_commit == commit


# --- WorkflowNode: no run state, compiler-only fields exist ----------------------


def test_workflow_node_has_no_run_state_or_position() -> None:
    fields = set(WorkflowNode.model_fields)
    for forbidden in ("status", "node_run_status", "position", "x", "y"):
        assert forbidden not in fields


def test_compiler_only_fields_present_on_shared_node() -> None:
    # These are compiler-only *values* but must exist on the shared node so a
    # CompiledGraph node can carry them. DraftValidator (HUB-110) rejects them
    # on author input; that boundary is not tested here.
    for field in (
        "effective_allowed_files",
        "effective_new_files",
        "effective_allowed_commands",
        "policy_risk_floor",
        "requires_changeset_approval",
        "test_kind",
        "test_argv",
    ):
        assert field in WorkflowNode.model_fields


def test_author_and_compiled_graphs_are_distinct_types() -> None:
    assert AuthorGraph is not CompiledGraph
    assert "integration_base_commit" not in AuthorGraph.model_fields
    assert "integration_base_commit" in CompiledGraph.model_fields
    assert "source_author_hash" not in AuthorGraph.model_fields


# --- Collection defaults are independent ----------------------------------------


def test_list_defaults_are_not_shared_between_instances() -> None:
    a = AuthorGraph()
    b = AuthorGraph()
    a.nodes.append(_minimal_node())
    assert a.nodes is not b.nodes
    assert b.nodes == []


# --- CapabilityGrant model validators -------------------------------------------


def _now() -> datetime:
    return datetime(2026, 7, 11, tzinfo=UTC)


def test_capability_grant_consume_fields_must_pair() -> None:
    with pytest.raises(ValidationError):
        CapabilityGrant(
            grant_id="g1",
            request_id="r1",
            target_task_id="t1",
            action="edit_project_config",
            resource="pyproject.toml",
            expires_at=_now(),
            consumed_at=_now(),  # missing consumed_fencing_token
        )


def test_capability_grant_consumed_and_revoked_mutually_exclusive() -> None:
    with pytest.raises(ValidationError):
        CapabilityGrant(
            grant_id="g1",
            request_id="r1",
            target_task_id="t1",
            action="edit_project_config",
            resource="pyproject.toml",
            expires_at=_now(),
            consumed_at=_now(),
            consumed_fencing_token=5,
            revoked_at=_now(),
            revocation_reason="conflict",
        )


def test_capability_grant_revoke_fields_must_pair() -> None:
    with pytest.raises(ValidationError):
        CapabilityGrant(
            grant_id="g1",
            request_id="r1",
            target_task_id="t1",
            action="edit_project_config",
            resource="pyproject.toml",
            expires_at=_now(),
            revoked_at=_now(),  # missing revocation_reason
        )


def test_capability_grant_valid_consumed() -> None:
    grant = CapabilityGrant(
        grant_id="g1",
        request_id="r1",
        target_task_id="t1",
        action="edit_project_config",
        resource="pyproject.toml",
        expires_at=_now(),
        consumed_at=_now(),
        consumed_fencing_token=7,
    )
    assert grant.consumed_fencing_token == 7


# --- EventEnvelope model validator ----------------------------------------------


class _Payload(StrictModel):
    detail: str = ""


def test_event_run_fields_must_pair() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope[_Payload](
            event_id=1,
            session_id="s1",
            workflow_id="wf1",
            workflow_run_id="run1",  # run_seq missing
            event_type="node.completed",
            actor_type=ActorType.SYSTEM,
            payload=_Payload(),
            created_at=_now(),
        )


def test_event_run_requires_workflow_id() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope[_Payload](
            event_id=1,
            session_id="s1",
            workflow_run_id="run1",
            run_seq=1,  # both run fields set but no workflow_id
            event_type="node.completed",
            actor_type=ActorType.SYSTEM,
            payload=_Payload(),
            created_at=_now(),
        )


def test_event_session_scoped_without_run_is_valid() -> None:
    event = EventEnvelope[_Payload](
        event_id=1,
        session_id="s1",
        event_type="session.created",
        actor_type=ActorType.USER,
        payload=_Payload(detail="ok"),
        created_at=_now(),
    )
    assert event.workflow_run_id is None
    assert event.run_seq is None


# --- Artifact owner exclusivity -------------------------------------------------


def test_artifact_owners_mutually_exclusive() -> None:
    with pytest.raises(ValidationError):
        Artifact(
            artifact_id="a1",
            session_id="s1",
            task_id="t1",
            planner_run_id="p1",
            artifact_type=ArtifactType.LOG,
            relative_path="artifacts/log-1.txt",
            sha256=PLACEHOLDER_SHA,
            size_bytes=1,
            redacted=True,
            created_at=_now(),
        )


# --- ConsoleChunk artifact-type / size checks -----------------------------------


def test_console_chunk_requires_console_artifact_type() -> None:
    with pytest.raises(ValidationError):
        ConsoleChunk(
            console_session_id="c1",
            seq=1,
            stream="stdout",
            artifact_ref=_artifact_ref(ArtifactType.LOG),
            size_bytes=10,
            created_at=_now(),
        )


def test_console_chunk_size_must_match_artifact() -> None:
    with pytest.raises(ValidationError):
        ConsoleChunk(
            console_session_id="c1",
            seq=1,
            stream="stdout",
            artifact_ref=_artifact_ref(ArtifactType.CONSOLE),
            size_bytes=999,  # artifact_ref.size_bytes is 10
            created_at=_now(),
        )


def test_console_chunk_valid() -> None:
    chunk = ConsoleChunk(
        console_session_id="c1",
        seq=1,
        stream="stdout",
        artifact_ref=_artifact_ref(ArtifactType.CONSOLE),
        size_bytes=10,
        created_at=_now(),
    )
    assert chunk.artifact_ref.artifact_type is ArtifactType.CONSOLE


# --- Approval discriminated union -----------------------------------------------


def _change_set_approval() -> ChangeSetApproval:
    return ChangeSetApproval(
        approval_id="ap1",
        workflow_run_id="run1",
        node_run_id="nr1",
        subject_sha256=PLACEHOLDER_SHA,
        effective_risk=RiskLevel.L2,
        expires_at=_now(),
        change_set_id="cs1",
        base_commit=PLACEHOLDER_COMMIT,
        patch_sha256=PLACEHOLDER_SHA,
        evidence_sha256=PLACEHOLDER_SHA,
    )


def test_approval_union_discriminates_by_subject_type() -> None:
    adapter: TypeAdapter[Approval] = TypeAdapter(Approval)
    parsed = adapter.validate_python(_change_set_approval().model_dump(mode="json"))
    assert isinstance(parsed, ChangeSetApproval)

    priv = PrivilegeApproval(
        approval_id="ap2",
        workflow_run_id="run1",
        node_run_id="nr1",
        subject_sha256=PLACEHOLDER_SHA,
        effective_risk=RiskLevel.L2,
        expires_at=_now(),
        privilege_request_id="pr1",
        evidence_sha256=PLACEHOLDER_SHA,
    )
    parsed_priv = adapter.validate_python(priv.model_dump(mode="json"))
    assert isinstance(parsed_priv, PrivilegeApproval)


# --- Canonical serialization ----------------------------------------------------


def test_canonical_json_is_key_order_independent() -> None:
    node_a = WorkflowNode(id="n1", node_type="agent_task", task_kind="implement", title="t")
    node_b = WorkflowNode(title="t", task_kind="implement", node_type="agent_task", id="n1")
    assert canonical_json(node_a) == canonical_json(node_b)


def test_canonical_json_changes_with_content() -> None:
    base = _change_set_approval()
    other = base.model_copy(update={"effective_risk": RiskLevel.L3})
    assert canonical_json(base) != canonical_json(other)


def test_canonical_json_round_trips_through_model() -> None:
    approval = _change_set_approval()
    raw = canonical_json(approval)
    restored = ChangeSetApproval.model_validate_json(raw)
    assert canonical_json(restored) == raw


def test_contract_version_is_frozen() -> None:
    assert CONTRACT_VERSION == "1"
    assert AuthorGraph().schema_version == CONTRACT_VERSION


# --- Required-text semantics (regression for over-defaulting) -------------------


def test_node_summary_summary_is_required() -> None:
    assert NodeSummary.model_fields["summary"].is_required()
    with pytest.raises(ValidationError):
        NodeSummary(node_run_id="nr1", status=NodeRunStatus.COMPLETED)


def test_next_suggestion_reason_is_required() -> None:
    assert NextSuggestion.model_fields["reason"].is_required()
    with pytest.raises(ValidationError):
        NextSuggestion(suggested_agent="agent-1")


def test_privilege_proposal_reason_is_required() -> None:
    assert PrivilegeRequestProposal.model_fields["reason"].is_required()
    with pytest.raises(ValidationError):
        PrivilegeRequestProposal(
            requested_capability="modify_config",
            requested_action="edit_project_config",
        )


def test_agent_output_envelope_summary_is_required() -> None:
    assert AgentOutputEnvelope.model_fields["summary"].is_required()
    with pytest.raises(ValidationError):
        AgentOutputEnvelope()


# --- Frozen models: assignment can never corrupt state --------------------------
#
# Regression for the validate_assignment bug: Pydantic runs after-model-validators
# *after* mutating the field, and does not roll back on failure. Models carrying a
# cross-field validator are frozen, so a rejected assignment leaves the original,
# still-valid object untouched (and any assignment raises).


def _valid_grant() -> CapabilityGrant:
    return CapabilityGrant(
        grant_id="g1",
        request_id="r1",
        target_task_id="t1",
        action="edit_project_config",
        resource="pyproject.toml",
        expires_at=_now(),
    )


def test_capability_grant_is_frozen() -> None:
    grant = _valid_grant()
    with pytest.raises(ValidationError):
        grant.consumed_at = _now()
    # The rejected assignment must not have mutated the object.
    assert grant.consumed_at is None
    assert grant.consumed_fencing_token is None


def test_artifact_is_frozen_and_uncorrupted_after_failed_assignment() -> None:
    artifact = Artifact(
        artifact_id="a1",
        session_id="s1",
        task_id="t1",
        artifact_type=ArtifactType.LOG,
        relative_path="artifacts/log-1.txt",
        sha256=PLACEHOLDER_SHA,
        size_bytes=1,
        redacted=True,
        created_at=_now(),
    )
    with pytest.raises(ValidationError):
        artifact.planner_run_id = "p1"  # would break owner exclusivity
    assert artifact.planner_run_id is None
    assert artifact.task_id == "t1"


def test_event_envelope_is_frozen() -> None:
    event = EventEnvelope[_Payload](
        event_id=1,
        session_id="s1",
        event_type="session.created",
        actor_type=ActorType.USER,
        payload=_Payload(detail="ok"),
        created_at=_now(),
    )
    with pytest.raises(ValidationError):
        event.workflow_run_id = "run1"  # would break run-field pairing
    assert event.workflow_run_id is None


def test_console_chunk_is_frozen() -> None:
    chunk = ConsoleChunk(
        console_session_id="c1",
        seq=1,
        stream="stdout",
        artifact_ref=_artifact_ref(ArtifactType.CONSOLE),
        size_bytes=10,
        created_at=_now(),
    )
    with pytest.raises(ValidationError):
        chunk.size_bytes = 999
    assert chunk.size_bytes == 10


def test_frozen_models_keep_extra_forbid() -> None:
    # Freezing must not weaken the strict base config.
    for model in (CapabilityGrant, Artifact, EventEnvelope, ConsoleChunk):
        assert model.model_config["extra"] == "forbid"
        assert model.model_config["frozen"] is True


def test_artifact_ref_is_frozen() -> None:
    # ArtifactRef is a content-addressed value object; it must be immutable so a
    # frozen owner (e.g. ConsoleChunk) cannot have its invariant broken via a
    # nested mutation of the ref it holds.
    ref = _artifact_ref(ArtifactType.CONSOLE)
    with pytest.raises(ValidationError):
        ref.size_bytes = 999
    assert ref.size_bytes == 10


def test_console_chunk_invariant_survives_nested_ref_mutation() -> None:
    # Regression for the nested-mutation hole: freezing ConsoleChunk alone is not
    # enough; its artifact_ref must also be immutable or the size/type invariant
    # could be broken from underneath.
    chunk = ConsoleChunk(
        console_session_id="c1",
        seq=1,
        stream="stdout",
        artifact_ref=_artifact_ref(ArtifactType.CONSOLE),
        size_bytes=10,
        created_at=_now(),
    )
    with pytest.raises(ValidationError):
        chunk.artifact_ref.size_bytes = 999
    with pytest.raises(ValidationError):
        chunk.artifact_ref.artifact_type = ArtifactType.LOG
    assert chunk.artifact_ref.size_bytes == 10
    assert chunk.artifact_ref.artifact_type is ArtifactType.CONSOLE


# --- Bounded collections / tokens -----------------------------------------------


def test_command_template_token_count_is_bounded() -> None:
    node = WorkflowNode(
        id="n1",
        node_type="agent_task",
        task_kind="implement",
        title="t",
        allowed_commands_candidate=[["pytest", "-q"]],
    )
    assert node.allowed_commands_candidate == [["pytest", "-q"]]
    with pytest.raises(ValidationError):
        WorkflowNode(
            id="n1",
            node_type="agent_task",
            task_kind="implement",
            title="t",
            allowed_commands_candidate=[["x"] * 65],  # exceeds 64-token cap
        )


def test_command_template_rejects_empty_vector() -> None:
    with pytest.raises(ValidationError):
        WorkflowNode(
            id="n1",
            node_type="agent_task",
            task_kind="implement",
            title="t",
            allowed_commands_candidate=[[]],  # a template needs at least the executable
        )


def test_context_pack_effective_commands_are_bounded_templates() -> None:
    pack = ContextPack(
        task_id="t1",
        node_id="n1",
        task_kind="implement",
        session_goal="g",
        current_node_title="title",
        current_task="do it",
        effective_allowed_commands=[["pytest"]],
    )
    assert pack.effective_allowed_commands == [["pytest"]]
    with pytest.raises(ValidationError):
        ContextPack(
            task_id="t1",
            node_id="n1",
            task_kind="implement",
            session_goal="g",
            current_node_title="title",
            current_task="do it",
            effective_allowed_commands=[["x"] * 65],
        )


def test_if_condition_value_token_is_bounded() -> None:
    ok = IfCondition(upstream_node_id="n1", field="status", operator="eq", value="completed")
    assert ok.value == "completed"
    with pytest.raises(ValidationError):
        IfCondition(upstream_node_id="n1", field="status", operator="eq", value="x" * 257)


def test_if_condition_value_list_count_is_bounded() -> None:
    with pytest.raises(ValidationError):
        IfCondition(
            upstream_node_id="n1",
            field="status",
            operator="in",
            value=["ok"] * 51,  # exceeds max_length=50
        )
