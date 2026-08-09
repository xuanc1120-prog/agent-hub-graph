"""Deterministic evidence manifest used by approval and merge gates."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from protocol import NodeRunStatus, RiskLevel, canonical_json
from storage.artifact_repository import ArtifactRepository
from storage.change_set_repository import ChangeSetRecord
from storage.workflow_run_repository import NodeRunRecord, WorkflowRunRecord


class ApprovalEvidenceError(RuntimeError):
    """The durable approval evidence cannot be reconstructed safely."""


async def build_manifest(
    *,
    run: WorkflowRunRecord,
    change_set: ChangeSetRecord,
    nodes: list[NodeRunRecord],
    artifacts: ArtifactRepository,
    runtime_policy_artifact_id: str | None = None,
) -> dict[str, Any]:
    source_record = next(
        (node for node in nodes if node.node_run_id == change_set.change_set.node_run_id),
        None,
    )
    if source_record is None:
        raise ApprovalEvidenceError("ChangeSet source node run is absent from the workflow run")
    source_id = source_record.node_id
    compiled = run.compiled_snapshot
    ancestors: set[str] = {source_id}
    while True:
        added = {
            edge.from_node
            for edge in compiled.edges
            if edge.to_node in ancestors and edge.from_node not in ancestors
        }
        if not added:
            break
        ancestors.update(added)
    evidence_nodes: list[dict[str, Any]] = []
    effective_risk = RiskLevel.L0
    risk_artifact_seen = False
    for node in sorted(nodes, key=lambda item: (item.node_id, item.attempt)):
        if node.node_id not in ancestors:
            continue
        compiled_node = next((item for item in compiled.nodes if item.id == node.node_id), None)
        if compiled_node is None:
            raise ApprovalEvidenceError("evidence node is absent from the compiled snapshot")
        node_entry: dict[str, Any] = {
            "node_run_id": node.node_run_id,
            "node_id": node.node_id,
            "node_type": node.node_type.value,
            "attempt": node.attempt,
            "status": node.status.value,
            "outcome": node.outcome.value if node.outcome is not None else None,
            "error_code": node.error_code,
            "output_artifact": None,
        }
        if node.output_artifact_id is not None:
            record, content = await artifacts.get_and_verify(
                node.output_artifact_id,
                expected_session_id=run.session_id,
            )
            node_entry["output_artifact"] = {
                "artifact_id": record.artifact_id,
                "artifact_type": record.artifact_type,
                "sha256": record.sha256,
                "size_bytes": record.size_bytes,
                "redacted": record.redacted,
            }
            if node.node_type.value == "risk_classifier":
                risk_artifact_seen = True
                try:
                    decoded = json.loads(content.decode("utf-8"))
                    reported = RiskLevel(str(decoded["effective_risk"]))
                except (KeyError, TypeError, ValueError, UnicodeDecodeError) as error:
                    raise ApprovalEvidenceError("risk evidence is not a valid report") from error
                effective_risk = max(
                    effective_risk,
                    reported,
                    key=lambda item: list(RiskLevel).index(item),
                )
        elif node.node_type.value in {"patch_guard", "command_guard", "test", "risk_classifier"}:
            if node.status in {NodeRunStatus.COMPLETED, NodeRunStatus.BLOCKED_BY_GUARD}:
                raise ApprovalEvidenceError("completed guard/test/risk node lacks evidence")
        evidence_nodes.append(node_entry)

    source = next((item for item in compiled.nodes if item.id == source_id), None)
    if source is None:
        raise ApprovalEvidenceError("ChangeSet source is absent from the compiled snapshot")
    effective_risk = max(
        effective_risk,
        source.policy_risk_floor or RiskLevel.L0,
        source.risk_level_hint,
        key=lambda item: list(RiskLevel).index(item),
    )
    runtime_policy: dict[str, Any] | None = None
    if runtime_policy_artifact_id is not None:
        record, _content = await artifacts.get_and_verify(
            runtime_policy_artifact_id,
            expected_session_id=run.session_id,
        )
        runtime_policy = {
            "artifact_id": record.artifact_id,
            "artifact_type": record.artifact_type,
            "sha256": record.sha256,
            "size_bytes": record.size_bytes,
            "redacted": record.redacted,
        }
    if (
        any(
            item["node_type"] == "risk_classifier"
            and item["status"] == NodeRunStatus.COMPLETED.value
            for item in evidence_nodes
        )
        and not risk_artifact_seen
    ):
        raise ApprovalEvidenceError("completed risk classifier lacks a durable report")

    manifest: dict[str, Any] = {
        "schema": "hub210-approval-evidence-v1",
        "workflow_run_id": run.workflow_run_id,
        "session_id": run.session_id,
        "compiled_snapshot_hash": run.compiled_snapshot_hash,
        "layout_snapshot_hash": run.layout_snapshot_hash,
        "policy_version": run.policy_version,
        "current_commit": run.current_commit,
        "change_set": {
            "change_set_id": change_set.change_set.change_set_id,
            "base_commit": change_set.change_set.base_commit,
            "pre_state_hash": change_set.change_set.pre_state_hash,
            "post_state_hash": change_set.change_set.post_state_hash,
            "patch_sha256": change_set.change_set.patch_sha256,
            "manifest_sha256": sha256(canonical_json(change_set.document)).hexdigest(),
            "evidence_refs": sorted(
                [
                    {
                        "artifact_id": ref.artifact_id,
                        "artifact_type": ref.artifact_type.value,
                        "sha256": ref.sha256,
                        "size_bytes": ref.size_bytes,
                    }
                    for ref in change_set.change_set.evidence_refs
                ],
                key=lambda item: item["artifact_id"],
            ),
        },
        "scope": sorted(
            set((source.effective_allowed_files or []) + (source.effective_new_files or []))
        ),
        "effective_risk": effective_risk.value,
        "runtime_policy": runtime_policy,
        "nodes": evidence_nodes,
    }
    return manifest


def manifest_hash(manifest: dict[str, Any]) -> str:
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


__all__ = ["ApprovalEvidenceError", "build_manifest", "manifest_hash"]
