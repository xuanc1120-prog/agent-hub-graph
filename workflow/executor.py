"""Deterministic execution of claimed node_runs from CompiledGraph snapshots."""

from __future__ import annotations

from hashlib import sha256

from protocol import ArtifactType, NodeOutcome, NodeRunStatus, NodeType, canonical_json
from storage.artifact_repository import ArtifactRepository
from storage.leases import MasterLease
from storage.repositories import SessionRepository
from storage.workflow_run_repository import (
    NodeRunRecord,
    WorkflowRunRepository,
    task_id_for_node,
)
from workflow.handlers.base import NodeExecutionContext, NodeHandlerResult
from workflow.registry import NodeRegistry


class GraphExecutor:
    def __init__(
        self,
        runs: WorkflowRunRepository,
        sessions: SessionRepository,
        artifacts: ArtifactRepository,
        registry: NodeRegistry,
    ) -> None:
        self._runs = runs
        self._sessions = sessions
        self._artifacts = artifacts
        self._registry = registry

    async def execute_claimed(
        self,
        claimed: NodeRunRecord,
        *,
        lease: MasterLease,
    ) -> NodeRunRecord:
        if claimed.status != NodeRunStatus.RUNNING:
            raise ValueError("GraphExecutor requires a claimed running node")
        run = await self._runs.get(claimed.workflow_run_id)
        node = next(
            (item for item in run.compiled_snapshot.nodes if item.id == claimed.node_id),
            None,
        )
        if node is None:
            return await self._runs.complete_node(
                claimed.node_run_id,
                target=NodeRunStatus.FAILED,
                outcome=NodeOutcome.FAILURE,
                summary="Claimed node is missing from immutable snapshot.",
                error_code="snapshot_node_missing",
                lease=lease,
            )
        session = await self._sessions.get(run.session_id)
        node_runs = await self._runs.list_nodes(run.workflow_run_id)
        by_node = {item.node_id: item for item in node_runs}
        incoming = [edge for edge in run.compiled_snapshot.edges if edge.to_node == node.id]
        predecessors = tuple(by_node[edge.from_node] for edge in incoming)
        active_predecessors = tuple(
            by_node[edge.from_node]
            for edge in incoming
            if _edge_satisfied(edge.condition.value, by_node[edge.from_node].outcome)
        )
        condition_upstream = (
            by_node.get(node.if_condition.upstream_node_id)
            if node.if_condition is not None
            else None
        )
        context = NodeExecutionContext(
            run=run,
            node_run=claimed,
            node=node,
            session=session,
            predecessors=predecessors,
            active_predecessors=active_predecessors,
            condition_upstream=condition_upstream,
            master_lease=lease,
        )
        handler = self._registry.get_handler(node)
        try:
            raw_result = await handler.execute(context)
            if not isinstance(raw_result, NodeHandlerResult):
                raise TypeError("NodeHandler returned an untyped result")
            result = raw_result
        except Exception:
            result = NodeHandlerResult(
                status=NodeRunStatus.FAILED,
                outcome=NodeOutcome.FAILURE,
                summary="NodeHandler raised an internal error; details were not persisted.",
                error_code="handler_exception",
            )

        output_artifact_id = None
        if result.artifact_refs:
            try:
                if node.node_type != NodeType.AGENT_TASK:
                    raise ValueError("only AgentTask handlers may return artifact references")
                expected_task_id = task_id_for_node(claimed.node_run_id)
                for ref in result.artifact_refs:
                    record = await self._artifacts.get(ref.artifact_id)
                    if (
                        record.session_id != run.session_id
                        or record.task_id != expected_task_id
                        or record.planner_run_id is not None
                        or record.artifact_type != ref.artifact_type.value
                        or record.relative_path != ref.relative_path
                        or record.sha256 != ref.sha256
                        or record.size_bytes != ref.size_bytes
                        or not record.redacted
                    ):
                        raise ValueError("handler artifact reference does not match storage")
                output_artifact_id = result.artifact_refs[0].artifact_id
            except Exception:
                result = NodeHandlerResult(
                    status=NodeRunStatus.FAILED,
                    outcome=NodeOutcome.FAILURE,
                    summary="NodeHandler returned an invalid artifact reference.",
                    error_code="handler_artifact_invalid",
                )
        if output_artifact_id is None:
            try:
                record = await self._artifacts.create(
                    artifact_id=_result_artifact_id(claimed.node_run_id, result),
                    session_id=run.session_id,
                    artifact_type=ArtifactType.REPORT,
                    content=canonical_json(result),
                    redacted=True,
                )
                output_artifact_id = record.artifact_id
            except Exception:
                result = NodeHandlerResult(
                    status=NodeRunStatus.FAILED,
                    outcome=NodeOutcome.FAILURE,
                    summary="Node result artifact could not be persisted.",
                    error_code="result_artifact_failed",
                )

        return await self._runs.complete_node(
            claimed.node_run_id,
            target=result.status,
            outcome=result.outcome,
            summary=result.summary,
            output_artifact_id=output_artifact_id,
            error_code=result.error_code,
            lease=lease,
        )


def _edge_satisfied(condition: str, outcome: NodeOutcome | None) -> bool:
    expected = {
        "success": {NodeOutcome.SUCCESS},
        "failure": {NodeOutcome.FAILURE, NodeOutcome.BLOCKED},
        "matched": {NodeOutcome.MATCHED},
        "not_matched": {NodeOutcome.NOT_MATCHED},
        "approved": {NodeOutcome.APPROVED},
        "rejected": {NodeOutcome.REJECTED},
    }
    return outcome in expected[condition]


def _result_artifact_id(node_run_id: str, result: NodeHandlerResult) -> str:
    digest = sha256(node_run_id.encode() + b"\x00" + canonical_json(result)).hexdigest()[:24]
    return f"node-result-{digest}"


__all__ = ["GraphExecutor"]
