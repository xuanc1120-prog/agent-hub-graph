"""Fenced workflow/node/task state machines backed by immutable snapshots."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import aiosqlite

from master.router import AgentCatalog
from protocol import (
    ActorType,
    AuthorGraph,
    ChangeSetStatus,
    CompiledGraph,
    EdgeCondition,
    NodeOutcome,
    NodeRunStatus,
    NodeType,
    TaskStatus,
    WorkflowLayout,
    WorkflowRunStatus,
    canonical_json,
)
from security.command_guard import CommandGuard
from security.path_policy import PathPolicy
from storage.agent_repository import AgentRepository, compute_agent_catalog_hash
from storage.db import Database, Transaction, utc_now_text
from storage.errors import ConcurrencyConflict, RecordNotFound, SnapshotIntegrityError
from storage.event_repository import EventRepository
from storage.leases import (
    MasterLease,
    MasterLeaseRepository,
    WorkspaceLease,
    WorkspaceLeaseRepository,
)
from storage.repositories import WorkflowRecord
from workflow.compiler import WorkflowCompiler
from workflow.events import (
    NODE_STATE_CHANGED,
    RECOVERY_ACTION,
    RUN_CREATED,
    RUN_STATE_CHANGED,
    TASK_STATE_CHANGED,
    NodeRunEventPayload,
    RecoveryEventPayload,
    TaskEventPayload,
    WorkflowRunEventPayload,
)

_NODE_TERMINAL = frozenset(
    {
        NodeRunStatus.BLOCKED_BY_GUARD,
        NodeRunStatus.FAILED,
        NodeRunStatus.COMPLETED,
        NodeRunStatus.SKIPPED,
        NodeRunStatus.SUPERSEDED,
        NodeRunStatus.CANCELLED,
        NodeRunStatus.ORPHANED,
    }
)
_RUN_TERMINAL = frozenset(
    {
        WorkflowRunStatus.BLOCKED,
        WorkflowRunStatus.FAILED,
        WorkflowRunStatus.COMPLETED,
        WorkflowRunStatus.CANCELLED,
        WorkflowRunStatus.ORPHANED,
    }
)


@dataclass(frozen=True, slots=True)
class NewWorkflowRun:
    workflow_run_id: str
    workflow: WorkflowRecord
    compiled_graph: CompiledGraph
    agent_catalog: AgentCatalog
    current_commit: str
    planner_run_id: str | None = None
    planner_id: str | None = None
    planner_model: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowRunRecord:
    workflow_run_id: str
    workflow_id: str
    session_id: str
    integration_base_commit: str
    current_commit: str
    workflow_semantic_version: int
    workflow_layout_version: int
    author_snapshot: AuthorGraph
    author_snapshot_hash: str
    compiled_snapshot: CompiledGraph
    compiled_snapshot_hash: str
    layout_snapshot: WorkflowLayout
    layout_snapshot_hash: str
    policy_version: str
    agent_catalog_snapshot: AgentCatalog
    agent_catalog_snapshot_hash: str
    planner_run_id: str | None
    planner_id: str | None
    planner_model: str | None
    status: WorkflowRunStatus
    created_at: str
    started_at: str | None
    finished_at: str | None
    cancel_requested_at: str | None
    merge_finalizing_at: str | None


@dataclass(frozen=True, slots=True)
class NodeRunRecord:
    node_run_id: str
    workflow_run_id: str
    node_id: str
    node_type: NodeType
    attempt: int
    status: NodeRunStatus
    outcome: NodeOutcome | None
    assigned_agent_id: str | None
    output_artifact_id: str | None
    error_code: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    node_run_id: str
    agent_id: str
    base_commit: str
    runtime_policy_artifact_id: str | None
    active_capability_grant_id: str | None
    status: TaskStatus
    created_at: str
    finished_at: str | None


class WorkflowRunRepository:
    def __init__(
        self,
        database: Database,
        events: EventRepository,
        leases: MasterLeaseRepository,
        workspace_leases: WorkspaceLeaseRepository,
        *,
        policy_version: str = "demo-v1",
    ) -> None:
        if not policy_version or len(policy_version) > 64:
            raise ValueError("policy_version must contain 1..64 characters")
        self._database = database
        self._events = events
        self._leases = leases
        self._workspace_leases = workspace_leases
        self._agents = AgentRepository(database)
        self._policy_version = policy_version

    async def create(
        self,
        value: NewWorkflowRun,
        *,
        lease: MasterLease,
        workspace_lease: WorkspaceLease,
        now: datetime | None = None,
    ) -> WorkflowRunRecord:
        workflow = value.workflow
        graph = value.compiled_graph
        if graph.source_author_hash != workflow.author_graph_hash:
            raise ValueError("compiled graph does not match workflow author snapshot")
        if graph.integration_base_commit != value.current_commit:
            raise ValueError("compiled graph base commit does not match run current commit")
        if graph.agent_catalog_snapshot_hash != value.agent_catalog.catalog_hash:
            raise ValueError("compiled graph agent catalog hash does not match snapshot")
        if value.planner_run_id != workflow.source_planner_run_id:
            raise ValueError("run planner lineage does not match workflow lineage")
        if value.planner_run_id is None and (
            value.planner_id is not None or value.planner_model is not None
        ):
            raise ValueError("planner metadata requires planner_run_id")
        actual_catalog_hash = compute_agent_catalog_hash(value.agent_catalog.agents)
        if actual_catalog_hash != value.agent_catalog.catalog_hash:
            raise SnapshotIntegrityError("agent catalog embedded hash is invalid")
        for node in graph.nodes:
            if node.node_type != NodeType.AGENT_TASK:
                continue
            spec = (
                value.agent_catalog.find_by_id(node.resolved_agent_id)
                if node.resolved_agent_id is not None
                else None
            )
            if spec is None or spec.spec_sha256 != node.resolved_agent_spec_sha256:
                raise ValueError(
                    f"compiled agent identity for node {node.id} is absent from catalog snapshot"
                )

        author_json = canonical_json(workflow.author_graph).decode("utf-8")
        submitted_compiled_json = canonical_json(graph)
        layout = _normalized_layout(workflow.layout)
        layout_json = canonical_json(layout).decode("utf-8")
        author_hash = sha256(author_json.encode()).hexdigest()
        layout_hash = sha256(layout_json.encode()).hexdigest()
        timestamp = utc_now_text(now)
        if author_hash != workflow.author_graph_hash:
            raise SnapshotIntegrityError("workflow author graph hash is invalid")
        if layout_hash != workflow.layout_hash:
            raise SnapshotIntegrityError("workflow layout hash is invalid")

        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            expected_resource = f"session:{workflow.session_id}:integration"
            if workspace_lease.resource_key != expected_resource:
                raise ValueError("workspace lease does not belong to the workflow session")
            await self._workspace_leases.assert_valid_in(
                transaction,
                workspace_lease,
                now=now,
            )
            current = await transaction.fetch_one(
                """
                SELECT w.session_id, w.semantic_version, w.layout_version,
                       w.author_graph_hash, w.layout_hash,
                       s.status AS session_status,
                       s.integration_head_commit,
                       s.shared_repo_path
                FROM workflows w
                JOIN sessions s ON w.session_id = s.id
                WHERE w.id = ?
                """,
                (workflow.workflow_id,),
            )
            if current is None:
                raise RecordNotFound(f"workflow not found: {workflow.workflow_id}")
            if (
                str(current["session_id"]) != workflow.session_id
                or int(current["semantic_version"]) != workflow.semantic_version
                or int(current["layout_version"]) != workflow.layout_version
                or str(current["author_graph_hash"]) != author_hash
                or str(current["layout_hash"]) != layout_hash
            ):
                raise ConcurrencyConflict("workflow changed before run snapshot creation")
            if str(current["session_status"]) != "active":
                raise ConcurrencyConflict("workflow run requires an active session")
            if str(current["integration_head_commit"]) != value.current_commit:
                raise ConcurrencyConflict("session integration HEAD changed before run creation")

            current_catalog = await self._agents.catalog_in(transaction)
            if current_catalog.catalog_hash != value.agent_catalog.catalog_hash:
                raise ConcurrencyConflict("agent catalog changed before run snapshot creation")
            authoritative = WorkflowCompiler(
                current_catalog,
                policy_version=self._policy_version,
                path_policy=PathPolicy(Path(str(current["shared_repo_path"]))),
                command_guard=CommandGuard(),
            ).compile(
                workflow.author_graph,
                integration_base_commit=value.current_commit,
            )
            if not authoritative.ok or authoritative.graph is None:
                raise SnapshotIntegrityError(
                    "authoritative workflow compilation failed before snapshot creation"
                )
            authoritative_compiled_json = canonical_json(authoritative.graph)
            if (
                authoritative.source_author_hash != author_hash
                or authoritative_compiled_json != submitted_compiled_json
            ):
                raise SnapshotIntegrityError(
                    "submitted compiled graph is not the deterministic workflow compilation"
                )
            graph = authoritative.graph
            compiled_json = authoritative_compiled_json.decode("utf-8")
            compiled_hash = sha256(authoritative_compiled_json).hexdigest()
            catalog_json = canonical_json(current_catalog).decode("utf-8")

            if value.planner_run_id is not None:
                planner = await transaction.fetch_one(
                    """
                    SELECT session_id, planner_id, planner_model, status,
                           result_workflow_id
                    FROM planner_runs WHERE id = ?
                    """,
                    (value.planner_run_id,),
                )
                if planner is None:
                    raise RecordNotFound(f"planner run not found: {value.planner_run_id}")
                if (
                    str(planner["session_id"]) != workflow.session_id
                    or str(planner["status"]) != "succeeded"
                    or str(planner["result_workflow_id"]) != workflow.workflow_id
                    or str(planner["planner_id"]) != value.planner_id
                ):
                    raise ValueError("planner run does not match the workflow run lineage")
                persisted_model = (
                    str(planner["planner_model"]) if planner["planner_model"] is not None else None
                )
                if persisted_model != value.planner_model:
                    raise ValueError("planner model does not match persisted lineage")

            await transaction.execute(
                """
                INSERT INTO workflow_runs(
                    id, workflow_id, session_id, integration_base_commit, current_commit,
                    workflow_semantic_version, workflow_layout_version,
                    author_snapshot_json, author_snapshot_hash,
                    compiled_snapshot_json, compiled_snapshot_hash,
                    layout_snapshot_json, layout_snapshot_hash,
                    policy_version, agent_catalog_snapshot_json,
                    agent_catalog_snapshot_hash, planner_run_id, planner_id, planner_model,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    value.workflow_run_id,
                    workflow.workflow_id,
                    workflow.session_id,
                    graph.integration_base_commit,
                    value.current_commit,
                    workflow.semantic_version,
                    workflow.layout_version,
                    author_json,
                    author_hash,
                    compiled_json,
                    compiled_hash,
                    layout_json,
                    layout_hash,
                    graph.policy_version,
                    catalog_json,
                    value.agent_catalog.catalog_hash,
                    value.planner_run_id,
                    value.planner_id,
                    value.planner_model,
                    timestamp,
                ),
            )
            for node in sorted(graph.nodes, key=lambda item: item.id):
                await transaction.execute(
                    """
                    INSERT INTO node_runs(
                        id, workflow_run_id, node_id, node_type, attempt, status,
                        assigned_agent_id, created_at
                    ) VALUES (?, ?, ?, ?, 1, 'pending', ?, ?)
                    """,
                    (
                        node_run_id(value.workflow_run_id, node.id, 1),
                        value.workflow_run_id,
                        node.id,
                        node.node_type.value,
                        node.resolved_agent_id,
                        timestamp,
                    ),
                )
            await self._events.append_in(
                transaction,
                session_id=workflow.session_id,
                workflow_id=workflow.workflow_id,
                workflow_run_id=value.workflow_run_id,
                event_type=RUN_CREATED,
                actor_type=ActorType.MASTER,
                actor_id=lease.instance_id,
                payload=WorkflowRunEventPayload(
                    master_fencing_token=lease.fencing_token,
                    workflow_run_id=value.workflow_run_id,
                    status=WorkflowRunStatus.PENDING,
                    compiled_snapshot_hash=compiled_hash,
                    planner_run_id=value.planner_run_id,
                    planner_id=value.planner_id,
                    planner_model=value.planner_model,
                ),
                now=now,
            )
            row = await transaction.fetch_one(
                "SELECT * FROM workflow_runs WHERE id = ?",
                (value.workflow_run_id,),
            )
        assert row is not None
        return _run_record(row)

    async def get(self, workflow_run_id: str) -> WorkflowRunRecord:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM workflow_runs WHERE id = ?",
                (workflow_run_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            raise RecordNotFound(f"workflow run not found: {workflow_run_id}")
        return _run_record(row)

    async def next_schedulable_run_id(self, *, lease: MasterLease) -> str | None:
        """Read the next durable run; claim mutations still fence in transaction."""

        await self._leases.assert_valid(lease)
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT id FROM workflow_runs
                WHERE status IN ('pending', 'running')
                  AND cancel_requested_at IS NULL
                ORDER BY created_at, id
                LIMIT 1
                """
            )
            row = await cursor.fetchone()
            await cursor.close()
        return str(row["id"]) if row is not None else None

    async def list_nodes(self, workflow_run_id: str) -> list[NodeRunRecord]:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM node_runs
                WHERE workflow_run_id = ?
                ORDER BY node_id, attempt
                """,
                (workflow_run_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [_node_record(row) for row in rows]

    async def start_and_reconcile(
        self,
        workflow_run_id: str,
        *,
        lease: MasterLease,
        now: datetime | None = None,
    ) -> WorkflowRunRecord:
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            row = await _run_row(transaction, workflow_run_id)
            _validated_snapshot_models(row)
            status = WorkflowRunStatus(str(row["status"]))
            if status == WorkflowRunStatus.PENDING:
                timestamp = utc_now_text(now)
                changed = await transaction.execute(
                    """
                    UPDATE workflow_runs SET status = 'running', started_at = ?
                    WHERE id = ? AND status = 'pending' AND cancel_requested_at IS NULL
                    """,
                    (timestamp, workflow_run_id),
                )
                if changed != 1:
                    raise ConcurrencyConflict("workflow run could not transition to running")
                await self._append_run_state(
                    transaction,
                    row,
                    lease,
                    previous=WorkflowRunStatus.PENDING,
                    target=WorkflowRunStatus.RUNNING,
                    now=now,
                )
                row = await _run_row(transaction, workflow_run_id)
                status = WorkflowRunStatus.RUNNING
            if status == WorkflowRunStatus.RUNNING:
                await self._reconcile_in(transaction, row, lease, now=now)
                row = await _run_row(transaction, workflow_run_id)
        return _run_record(row)

    async def claim_next(
        self,
        workflow_run_id: str,
        *,
        lease: MasterLease,
        now: datetime | None = None,
    ) -> NodeRunRecord | None:
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            run = await _run_row(transaction, workflow_run_id)
            _author, graph, _layout, _catalog = _validated_snapshot_models(run)
            if WorkflowRunStatus(str(run["status"])) != WorkflowRunStatus.RUNNING:
                return None
            if run["cancel_requested_at"] is not None:
                return None
            rows = await transaction.fetch_all(
                "SELECT * FROM node_runs WHERE workflow_run_id = ?",
                (workflow_run_id,),
            )
            by_node = {str(row["node_id"]): row for row in rows}
            selected = next(
                (
                    by_node[node_id]
                    for node_id in _topological_order(graph)
                    if NodeRunStatus(str(by_node[node_id]["status"])) == NodeRunStatus.READY
                ),
                None,
            )
            if selected is None:
                return None
            changed = await transaction.execute(
                """
                UPDATE node_runs SET status = 'running', started_at = ?
                WHERE id = ? AND status = 'ready'
                """,
                (timestamp, str(selected["id"])),
            )
            if changed != 1:
                raise ConcurrencyConflict("ready node was claimed concurrently")
            await self._append_node_state(
                transaction,
                run,
                lease,
                node_run_id=str(selected["id"]),
                node_id=str(selected["node_id"]),
                previous=NodeRunStatus.READY,
                target=NodeRunStatus.RUNNING,
                now=now,
            )
            claimed = await transaction.fetch_one(
                "SELECT * FROM node_runs WHERE id = ?",
                (str(selected["id"]),),
            )
        assert claimed is not None
        return _node_record(claimed)

    async def complete_node(
        self,
        node_run_id_value: str,
        *,
        target: NodeRunStatus,
        outcome: NodeOutcome,
        summary: str,
        lease: MasterLease,
        output_artifact_id: str | None = None,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> NodeRunRecord:
        if target not in {
            NodeRunStatus.COMPLETED,
            NodeRunStatus.FAILED,
            NodeRunStatus.BLOCKED_BY_GUARD,
        }:
            raise ValueError(f"invalid handler terminal status: {target.value}")
        allowed_outcomes = {
            NodeRunStatus.COMPLETED: {
                NodeOutcome.SUCCESS,
                NodeOutcome.MATCHED,
                NodeOutcome.NOT_MATCHED,
                NodeOutcome.APPROVED,
                NodeOutcome.REJECTED,
            },
            NodeRunStatus.FAILED: {NodeOutcome.FAILURE},
            NodeRunStatus.BLOCKED_BY_GUARD: {NodeOutcome.BLOCKED},
        }
        if outcome not in allowed_outcomes[target]:
            raise ValueError("node terminal status and outcome are inconsistent")
        if target != NodeRunStatus.COMPLETED and not error_code:
            raise ValueError("failed or blocked node completion requires error_code")
        timestamp = utc_now_text(now)
        finished_at = timestamp
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            current = await transaction.fetch_one(
                """
                SELECT nr.*, wr.workflow_id, wr.session_id, wr.status AS run_status,
                       wr.cancel_requested_at, wr.compiled_snapshot_json,
                       wr.compiled_snapshot_hash
                FROM node_runs nr
                JOIN workflow_runs wr ON nr.workflow_run_id = wr.id
                WHERE nr.id = ?
                """,
                (node_run_id_value,),
            )
            if current is None:
                raise RecordNotFound(f"node run not found: {node_run_id_value}")
            if current["cancel_requested_at"] is not None:
                raise ConcurrencyConflict("workflow cancellation prevents node completion")
            if WorkflowRunStatus(str(current["run_status"])) != WorkflowRunStatus.RUNNING:
                raise ConcurrencyConflict("node completion requires a running workflow")
            if output_artifact_id is not None:
                artifact = await transaction.fetch_one(
                    """
                    SELECT session_id, redacted, task_id, planner_run_id
                    FROM artifacts WHERE id = ?
                    """,
                    (output_artifact_id,),
                )
                if artifact is None:
                    raise RecordNotFound(f"output artifact not found: {output_artifact_id}")
                graph = CompiledGraph.model_validate_json(
                    str(current["compiled_snapshot_json"]),
                    strict=True,
                )
                if sha256(canonical_json(graph)).hexdigest() != str(
                    current["compiled_snapshot_hash"]
                ):
                    raise SnapshotIntegrityError("compiled workflow snapshot hash mismatch")
                compiled_node = next(
                    (node for node in graph.nodes if node.id == str(current["node_id"])),
                    None,
                )
                if compiled_node is None:
                    raise SnapshotIntegrityError(
                        "running node is absent from compiled workflow snapshot"
                    )
                owner_node_run_id = node_run_id_value
                source_bound = False
                if compiled_node.system_managed and compiled_node.source_node_id is not None:
                    source_bound = True
                    owner = await transaction.fetch_one(
                        """
                        SELECT id FROM node_runs
                        WHERE workflow_run_id = ? AND node_id = ?
                        ORDER BY attempt DESC LIMIT 1
                        """,
                        (
                            str(current["workflow_run_id"]),
                            compiled_node.source_node_id,
                        ),
                    )
                    if owner is None:
                        raise SnapshotIntegrityError(
                            "system node source run is absent from workflow run"
                        )
                    owner_node_run_id = str(owner["id"])
                task_owner = await transaction.fetch_one(
                    "SELECT id FROM tasks WHERE node_run_id = ?",
                    (owner_node_run_id,),
                )
                node_type = NodeType(str(current["node_type"]))
                if task_owner is None and (
                    source_bound
                    or (node_type == NodeType.AGENT_TASK and target == NodeRunStatus.COMPLETED)
                ):
                    raise SnapshotIntegrityError("node output requires an existing source task")
                expected_task_id = str(task_owner["id"]) if task_owner is not None else None
                actual_task_id = (
                    str(artifact["task_id"]) if artifact["task_id"] is not None else None
                )
                if (
                    str(artifact["session_id"]) != str(current["session_id"])
                    or not bool(artifact["redacted"])
                    or artifact["planner_run_id"] is not None
                    or (actual_task_id is not None and actual_task_id != expected_task_id)
                ):
                    raise ValueError("node output artifact is not a redacted session artifact")
                requires_owned_output = (
                    target == NodeRunStatus.COMPLETED and node_type == NodeType.AGENT_TASK
                ) or (source_bound and target != NodeRunStatus.FAILED)
                if requires_owned_output and actual_task_id != expected_task_id:
                    raise ValueError("node output artifact must belong to its source task")
            if NodeType(str(current["node_type"])) == NodeType.AGENT_TASK:
                graph = CompiledGraph.model_validate_json(
                    str(current["compiled_snapshot_json"]),
                    strict=True,
                )
                if sha256(canonical_json(graph)).hexdigest() != str(
                    current["compiled_snapshot_hash"]
                ):
                    raise SnapshotIntegrityError("compiled workflow snapshot hash mismatch")
                compiled_node = next(
                    (node for node in graph.nodes if node.id == str(current["node_id"])),
                    None,
                )
                if compiled_node is None:
                    raise SnapshotIntegrityError(
                        "running node is absent from compiled workflow snapshot"
                    )
                if compiled_node.requires_write:
                    task_state = await transaction.fetch_one(
                        """
                        SELECT t.id AS task_id,
                               t.status AS task_status,
                               cs.status AS change_set_status
                        FROM tasks t
                        LEFT JOIN change_sets cs ON cs.task_id = t.id
                        WHERE t.node_run_id = ?
                        """,
                        (node_run_id_value,),
                    )
                    if task_state is not None:
                        task_status = TaskStatus(str(task_state["task_status"]))
                        change_set_status = (
                            ChangeSetStatus(str(task_state["change_set_status"]))
                            if task_state["change_set_status"] is not None
                            else None
                        )
                        if target == NodeRunStatus.COMPLETED:
                            if (
                                task_status != TaskStatus.SUCCEEDED
                                or change_set_status != ChangeSetStatus.CAPTURED
                            ):
                                raise SnapshotIntegrityError(
                                    "completed write node lacks a durable successful task"
                                )
                        elif (
                            task_status
                            in {TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.SUCCEEDED}
                            or change_set_status == ChangeSetStatus.CAPTURED
                        ):
                            raise SnapshotIntegrityError(
                                "failed write node has an unfinished task or live ChangeSet"
                            )
                        task_event = await transaction.fetch_one(
                            """
                            SELECT payload_json FROM events
                            WHERE workflow_run_id = ?
                              AND event_type = ?
                              AND json_extract(payload_json, '$.task_id') = ?
                            ORDER BY run_seq DESC
                            LIMIT 1
                            """,
                            (
                                str(current["workflow_run_id"]),
                                TASK_STATE_CHANGED,
                                str(task_state["task_id"]),
                            ),
                        )
                        if task_event is None:
                            raise SnapshotIntegrityError(
                                "write task terminal state lacks a fencing event"
                            )
                        try:
                            task_proof = TaskEventPayload.model_validate_json(
                                str(task_event["payload_json"]),
                                strict=True,
                            )
                        except ValueError as error:
                            raise SnapshotIntegrityError(
                                "write task fencing event is invalid"
                            ) from error
                        if (
                            task_proof.status != task_status
                            or task_proof.workspace_fencing_token is None
                        ):
                            raise SnapshotIntegrityError(
                                "write task terminal state lacks a matching fencing token"
                            )
                        workspace_lock = await transaction.fetch_one(
                            """
                            SELECT owner_kind, owner_operation_id, fencing_token
                            FROM file_locks WHERE resource_key = ?
                            """,
                            (f"session:{current['session_id']}:integration",),
                        )
                        if (
                            workspace_lock is None
                            or str(workspace_lock["owner_kind"]) != "agent_task"
                            or str(workspace_lock["owner_operation_id"])
                            != str(task_state["task_id"])
                            or int(workspace_lock["fencing_token"])
                            != task_proof.workspace_fencing_token
                        ):
                            raise SnapshotIntegrityError(
                                "workspace fencing advanced before write node completion"
                            )
                    elif target == NodeRunStatus.COMPLETED:
                        raise SnapshotIntegrityError("completed write node lacks a durable task")

            changed = await transaction.execute(
                """
                UPDATE node_runs
                SET status = ?, outcome = ?, output_artifact_id = ?,
                    error_code = ?, finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    target.value,
                    outcome.value,
                    output_artifact_id,
                    error_code,
                    finished_at,
                    node_run_id_value,
                ),
            )
            if changed != 1:
                raise ConcurrencyConflict("node completion lost its running CAS")
            await self._append_node_state(
                transaction,
                current,
                lease,
                node_run_id=node_run_id_value,
                node_id=str(current["node_id"]),
                previous=NodeRunStatus.RUNNING,
                target=target,
                outcome=outcome,
                summary=summary,
                error_code=error_code,
                now=now,
            )
            row = await transaction.fetch_one(
                "SELECT * FROM node_runs WHERE id = ?",
                (node_run_id_value,),
            )
        assert row is not None
        return _node_record(row)

    async def create_task(
        self,
        *,
        task_id: str,
        node_run_id_value: str,
        agent_id: str,
        base_commit: str,
        lease: MasterLease,
        now: datetime | None = None,
    ) -> TaskRecord:
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            node = await transaction.fetch_one(
                """
                SELECT nr.*, wr.workflow_id, wr.session_id,
                       wr.status AS run_status, wr.cancel_requested_at,
                       wr.current_commit
                FROM node_runs nr
                JOIN workflow_runs wr ON nr.workflow_run_id = wr.id
                WHERE nr.id = ?
                """,
                (node_run_id_value,),
            )
            if node is None:
                raise RecordNotFound(f"node run not found: {node_run_id_value}")
            if NodeRunStatus(str(node["status"])) != NodeRunStatus.RUNNING:
                raise ConcurrencyConflict("task can only be created for a running node")
            if (
                WorkflowRunStatus(str(node["run_status"])) != WorkflowRunStatus.RUNNING
                or node["cancel_requested_at"] is not None
            ):
                raise ConcurrencyConflict("task creation requires a running workflow")
            if str(node["assigned_agent_id"]) != agent_id:
                raise ValueError("task agent does not match compiled node assignment")
            if str(node["current_commit"]) != base_commit:
                raise ValueError("task base commit does not match workflow current commit")
            await transaction.execute(
                """
                INSERT INTO tasks(id, node_run_id, agent_id, base_commit, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (task_id, node_run_id_value, agent_id, base_commit, timestamp),
            )
            await self._append_task_state(
                transaction,
                node,
                lease,
                task_id=task_id,
                previous=None,
                target=TaskStatus.PENDING,
                now=now,
            )
            row = await transaction.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        assert row is not None
        return _task_record(row)

    async def create_and_start_task(
        self,
        *,
        task_id: str,
        node_run_id_value: str,
        agent_id: str,
        base_commit: str,
        runtime_policy_artifact_id: str,
        lease: MasterLease,
        now: datetime | None = None,
    ) -> TaskRecord:
        """Atomically create a task, bind its policy artifact, and start it."""

        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            node = await transaction.fetch_one(
                """
                SELECT nr.*, wr.workflow_id, wr.session_id, wr.cancel_requested_at,
                       wr.status AS run_status, wr.current_commit
                FROM node_runs nr
                JOIN workflow_runs wr ON nr.workflow_run_id = wr.id
                WHERE nr.id = ?
                """,
                (node_run_id_value,),
            )
            if node is None:
                raise RecordNotFound(f"node run not found: {node_run_id_value}")
            if NodeRunStatus(str(node["status"])) != NodeRunStatus.RUNNING:
                raise ConcurrencyConflict("task can only be created for a running node")
            if node["cancel_requested_at"] is not None:
                raise ConcurrencyConflict("workflow cancellation prevents task creation")
            if WorkflowRunStatus(str(node["run_status"])) != WorkflowRunStatus.RUNNING:
                raise ConcurrencyConflict("task creation requires a running workflow")
            if str(node["assigned_agent_id"]) != agent_id:
                raise ValueError("task agent does not match compiled node assignment")
            if str(node["current_commit"]) != base_commit:
                raise ValueError("task base commit does not match workflow current commit")
            policy = await transaction.fetch_one(
                """
                SELECT session_id, task_id, planner_run_id, artifact_type, redacted
                FROM artifacts WHERE id = ?
                """,
                (runtime_policy_artifact_id,),
            )
            if policy is None:
                raise RecordNotFound(
                    f"runtime policy artifact not found: {runtime_policy_artifact_id}"
                )
            if (
                str(policy["session_id"]) != str(node["session_id"])
                or (policy["task_id"] is not None and str(policy["task_id"]) != task_id)
                or policy["planner_run_id"] is not None
                or str(policy["artifact_type"]) != "runtime_policy"
                or not bool(policy["redacted"])
            ):
                raise ValueError("runtime policy artifact is not an unowned task policy")

            existing_task = await transaction.fetch_one(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            )
            if existing_task is None:
                await transaction.execute(
                    """
                    INSERT INTO tasks(
                        id, node_run_id, agent_id, base_commit,
                        runtime_policy_artifact_id, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'running', ?)
                    """,
                    (
                        task_id,
                        node_run_id_value,
                        agent_id,
                        base_commit,
                        runtime_policy_artifact_id,
                        timestamp,
                    ),
                )
                claimed = await transaction.execute(
                    """
                    UPDATE artifacts SET task_id = ?
                    WHERE id = ? AND task_id IS NULL AND planner_run_id IS NULL
                    """,
                    (task_id, runtime_policy_artifact_id),
                )
                if claimed != 1:
                    raise ConcurrencyConflict("runtime policy artifact ownership changed")
                await self._append_task_state(
                    transaction,
                    node,
                    lease,
                    task_id=task_id,
                    previous=None,
                    target=TaskStatus.PENDING,
                    now=now,
                )
            else:
                if (
                    str(existing_task["node_run_id"]) != node_run_id_value
                    or str(existing_task["agent_id"]) != agent_id
                    or str(existing_task["base_commit"]) != base_commit
                    or (
                        existing_task["runtime_policy_artifact_id"] is not None
                        and str(existing_task["runtime_policy_artifact_id"])
                        != runtime_policy_artifact_id
                    )
                    or TaskStatus(str(existing_task["status"])) != TaskStatus.PENDING
                ):
                    raise ConcurrencyConflict("precreated privilege retry task is not pending")
                if existing_task["runtime_policy_artifact_id"] is None:
                    claimed = await transaction.execute(
                        """
                        UPDATE artifacts SET task_id = ?
                        WHERE id = ? AND task_id IS NULL AND planner_run_id IS NULL
                        """,
                        (task_id, runtime_policy_artifact_id),
                    )
                    if claimed != 1:
                        raise ConcurrencyConflict("runtime policy artifact ownership changed")
                    bound = await transaction.execute(
                        """
                        UPDATE tasks SET runtime_policy_artifact_id = ?
                        WHERE id = ? AND runtime_policy_artifact_id IS NULL
                        """,
                        (runtime_policy_artifact_id, task_id),
                    )
                    if bound != 1:
                        raise ConcurrencyConflict("retry policy binding lost CAS")
                changed = await transaction.execute(
                    "UPDATE tasks SET status = 'running', finished_at = NULL "
                    "WHERE id = ? AND status = 'pending'",
                    (task_id,),
                )
                if changed != 1:
                    raise ConcurrencyConflict("precreated task start lost CAS")
                await self._append_task_state(
                    transaction,
                    node,
                    lease,
                    task_id=task_id,
                    previous=TaskStatus.PENDING,
                    target=TaskStatus.RUNNING,
                    now=now,
                )
            if existing_task is None:
                await self._append_task_state(
                    transaction,
                    node,
                    lease,
                    task_id=task_id,
                    previous=TaskStatus.PENDING,
                    target=TaskStatus.RUNNING,
                    now=now,
                )
            row = await transaction.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        assert row is not None
        return _task_record(row)

    async def prepare_privilege_retry(
        self,
        workflow_run_id: str,
        node_run_id_value: str,
        *,
        approval_id: str,
        lease: MasterLease,
        workspace_lease: WorkspaceLease,
        now: datetime | None = None,
    ) -> str:
        """Persist an attempt+1 target before a privilege approval is decided."""

        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            current = await transaction.fetch_one(
                """
                SELECT nr.*, wr.workflow_id, wr.session_id, wr.status AS run_status,
                       wr.cancel_requested_at, wr.current_commit,
                       t.id AS task_id, t.agent_id, t.base_commit,
                       t.runtime_policy_artifact_id, t.status AS task_status
                FROM node_runs nr
                JOIN workflow_runs wr ON wr.id = nr.workflow_run_id
                JOIN tasks t ON t.node_run_id = nr.id
                WHERE nr.id = ? AND nr.workflow_run_id = ?
                """,
                (node_run_id_value, workflow_run_id),
            )
            if current is None:
                raise RecordNotFound(f"privilege source node not found: {node_run_id_value}")
            expected_resource = f"session:{current['session_id']}:integration"
            if (
                workspace_lease.resource_key != expected_resource
                or workspace_lease.owner_kind != "agent_task"
                or workspace_lease.owner_operation_id != str(current["task_id"])
            ):
                raise ValueError("privilege retry requires the source task workspace lease")
            await self._workspace_leases.assert_valid_in(transaction, workspace_lease, now=now)
            if current["cancel_requested_at"] is not None:
                raise ConcurrencyConflict("workflow cancellation prevents privilege retry")
            if (
                NodeRunStatus(str(current["status"])) != NodeRunStatus.RUNNING
                or WorkflowRunStatus(str(current["run_status"])) != WorkflowRunStatus.RUNNING
                or TaskStatus(str(current["task_status"])) != TaskStatus.RUNNING
            ):
                raise ConcurrencyConflict("privilege retry requires a running source task")
            target_node_id = node_run_id(
                workflow_run_id,
                str(current["node_id"]),
                int(current["attempt"]) + 1,
            )
            target_task_id = task_id_for_node(target_node_id)
            existing = await transaction.fetch_one(
                "SELECT id FROM node_runs WHERE id = ?", (target_node_id,)
            )
            if existing is not None:
                return target_task_id
            await transaction.execute(
                """
                INSERT INTO node_runs(
                    id, workflow_run_id, node_id, node_type, attempt, status,
                    assigned_agent_id, created_at, error_code
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    target_node_id,
                    workflow_run_id,
                    str(current["node_id"]),
                    str(current["node_type"]),
                    int(current["attempt"]) + 1,
                    str(current["assigned_agent_id"]),
                    timestamp,
                    f"privilege_retry_for:{approval_id}",
                ),
            )
            await transaction.execute(
                """
                INSERT INTO tasks(
                    id, node_run_id, agent_id, base_commit,
                    runtime_policy_artifact_id, status, created_at
                ) VALUES (?, ?, ?, ?, NULL, 'pending', ?)
                """,
                (
                    target_task_id,
                    target_node_id,
                    str(current["agent_id"]),
                    str(current["base_commit"]),
                    timestamp,
                ),
            )
            changed = await transaction.execute(
                """
                UPDATE tasks SET status = 'privilege_requested', finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (timestamp, str(current["task_id"])),
            )
            if changed != 1:
                raise ConcurrencyConflict("privilege request lost source task CAS")
            changed = await transaction.execute(
                """
                UPDATE node_runs SET status = 'waiting_approval',
                    error_code = ?, finished_at = NULL
                WHERE id = ? AND status = 'running'
                """,
                (f"privilege_pending:{approval_id}", node_run_id_value),
            )
            if changed != 1:
                raise ConcurrencyConflict("privilege wait lost source node CAS")
            changed = await transaction.execute(
                """
                UPDATE workflow_runs SET status = 'waiting_approval'
                WHERE id = ? AND status = 'running' AND cancel_requested_at IS NULL
                """,
                (workflow_run_id,),
            )
            if changed != 1:
                raise ConcurrencyConflict("privilege wait lost workflow CAS")
            await self._append_task_state(
                transaction,
                current,
                lease,
                task_id=str(current["task_id"]),
                previous=TaskStatus.RUNNING,
                target=TaskStatus.PRIVILEGE_REQUESTED,
                error_code=f"privilege_pending:{approval_id}",
                workspace_fencing_token=workspace_lease.fencing_token,
                now=now,
            )
            await self._append_node_state(
                transaction,
                current,
                lease,
                node_run_id=node_run_id_value,
                node_id=str(current["node_id"]),
                previous=NodeRunStatus.RUNNING,
                target=NodeRunStatus.WAITING_APPROVAL,
                summary="Waiting for privilege approval.",
                error_code=f"privilege_pending:{approval_id}",
                now=now,
            )
            await self._append_run_state(
                transaction,
                current,
                lease,
                previous=WorkflowRunStatus.RUNNING,
                target=WorkflowRunStatus.WAITING_APPROVAL,
                now=now,
            )
        return target_task_id

    async def finish_task(
        self,
        task_id: str,
        *,
        target: TaskStatus,
        lease: MasterLease,
        workspace_lease: WorkspaceLease | None = None,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> TaskRecord:
        if target not in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
            TaskStatus.CANCELLED,
            TaskStatus.BLOCKED_BY_GUARD,
            TaskStatus.PARSE_FAILED,
            TaskStatus.PRIVILEGE_REQUESTED,
            TaskStatus.ORPHANED,
        }:
            raise ValueError(f"invalid task terminal status: {target.value}")
        if target not in {TaskStatus.SUCCEEDED, TaskStatus.PRIVILEGE_REQUESTED} and not error_code:
            raise ValueError("failed, cancelled, or blocked task completion requires error_code")
        return await self._transition_task(
            task_id,
            expected=TaskStatus.RUNNING,
            target=target,
            lease=lease,
            error_code=error_code,
            workspace_lease=workspace_lease,
            now=now,
        )

    async def get_task(self, task_id: str) -> TaskRecord:
        async with self._database.connection() as connection:
            cursor = await connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            raise RecordNotFound(f"task not found: {task_id}")
        return _task_record(row)

    async def _transition_task(
        self,
        task_id: str,
        *,
        expected: TaskStatus,
        target: TaskStatus,
        lease: MasterLease,
        error_code: str | None = None,
        workspace_lease: WorkspaceLease | None = None,
        now: datetime | None = None,
    ) -> TaskRecord:
        finished_at = None if target == TaskStatus.RUNNING else utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            current = await transaction.fetch_one(
                """
                SELECT t.*, nr.workflow_run_id, nr.node_id,
                       nr.status AS node_status,
                       wr.workflow_id, wr.session_id, wr.cancel_requested_at,
                       wr.status AS run_status, wr.compiled_snapshot_json,
                       wr.compiled_snapshot_hash
                FROM tasks t
                JOIN node_runs nr ON t.node_run_id = nr.id
                JOIN workflow_runs wr ON nr.workflow_run_id = wr.id
                WHERE t.id = ?
                """,
                (task_id,),
            )
            if current is None:
                raise RecordNotFound(f"task not found: {task_id}")
            if current["cancel_requested_at"] is not None:
                raise ConcurrencyConflict("workflow cancellation prevents task transition")
            if WorkflowRunStatus(str(current["run_status"])) != WorkflowRunStatus.RUNNING:
                raise ConcurrencyConflict("task transition requires a running workflow")
            if NodeRunStatus(str(current["node_status"])) != NodeRunStatus.RUNNING:
                raise ConcurrencyConflict("task transition requires a running node")
            graph = CompiledGraph.model_validate_json(
                str(current["compiled_snapshot_json"]),
                strict=True,
            )
            if sha256(canonical_json(graph)).hexdigest() != str(current["compiled_snapshot_hash"]):
                raise SnapshotIntegrityError("compiled workflow snapshot hash mismatch")
            compiled_node = next(
                (node for node in graph.nodes if node.id == str(current["node_id"])),
                None,
            )
            if compiled_node is None:
                raise SnapshotIntegrityError("task node is absent from compiled snapshot")
            write_task = compiled_node.node_type == NodeType.AGENT_TASK and bool(
                compiled_node.requires_write
            )
            if write_task:
                if workspace_lease is None:
                    raise ValueError("write task completion requires a workspace lease")
                expected_resource = f"session:{current['session_id']}:integration"
                if (
                    workspace_lease.resource_key != expected_resource
                    or workspace_lease.owner_kind != "agent_task"
                    or workspace_lease.owner_operation_id != task_id
                ):
                    raise ValueError("workspace lease does not own this write task")
                await self._workspace_leases.assert_valid_in(
                    transaction,
                    workspace_lease,
                    now=now,
                )
                change_set = await transaction.fetch_one(
                    "SELECT status FROM change_sets WHERE task_id = ?",
                    (task_id,),
                )
                change_set_status = (
                    ChangeSetStatus(str(change_set["status"])) if change_set is not None else None
                )
                if target == TaskStatus.SUCCEEDED:
                    if change_set_status != ChangeSetStatus.CAPTURED:
                        raise SnapshotIntegrityError(
                            "successful write task requires a captured ChangeSet"
                        )
                elif change_set_status == ChangeSetStatus.CAPTURED:
                    raise SnapshotIntegrityError(
                        "failed write task cannot retain a captured ChangeSet"
                    )
            elif workspace_lease is not None:
                raise ValueError("read-only task completion cannot use a workspace lease")
            changed = await transaction.execute(
                """
                UPDATE tasks
                SET status = ?, finished_at = ?
                WHERE id = ? AND status = ?
                """,
                (target.value, finished_at, task_id, expected.value),
            )
            if changed != 1:
                raise ConcurrencyConflict(
                    f"task {task_id} expected {expected.value}, found {current['status']}"
                )
            await self._append_task_state(
                transaction,
                current,
                lease,
                task_id=task_id,
                previous=expected,
                target=target,
                error_code=error_code,
                workspace_fencing_token=(
                    workspace_lease.fencing_token if workspace_lease is not None else None
                ),
                now=now,
            )
            row = await transaction.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        assert row is not None
        return _task_record(row)

    async def enter_waiting_approval(
        self,
        node_run_id_value: str,
        *,
        approval_id: str,
        lease: MasterLease,
        now: datetime | None = None,
    ) -> NodeRunRecord:
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            current = await transaction.fetch_one(
                """
                SELECT nr.*, wr.workflow_id, wr.session_id, wr.status AS run_status,
                       wr.cancel_requested_at, wr.merge_finalizing_at
                FROM node_runs nr JOIN workflow_runs wr ON nr.workflow_run_id = wr.id
                WHERE nr.id = ?
                """,
                (node_run_id_value,),
            )
            if current is None:
                raise RecordNotFound(f"node run not found: {node_run_id_value}")
            if (
                NodeRunStatus(str(current["status"])) != NodeRunStatus.RUNNING
                or WorkflowRunStatus(str(current["run_status"])) != WorkflowRunStatus.RUNNING
            ):
                raise ConcurrencyConflict("approval wait requires a running node and workflow")
            if current["cancel_requested_at"] is not None:
                raise ConcurrencyConflict("cancellation prevents approval wait")
            approval = await transaction.fetch_one(
                "SELECT status FROM approvals WHERE id = ?",
                (approval_id,),
            )
            if approval is not None and str(approval["status"]) in {"approved", "rejected"}:
                return await self.resume_after_approval_in(
                    transaction,
                    str(current["workflow_run_id"]),
                    node_run_id_value,
                    approved=str(approval["status"]) == "approved",
                    lease=lease,
                    now=now,
                )
            changed = await transaction.execute(
                """
                UPDATE node_runs SET status = 'waiting_approval',
                    error_code = ?, finished_at = NULL
                WHERE id = ? AND status = 'running'
                """,
                (f"approval_pending:{approval_id}", node_run_id_value),
            )
            if changed != 1:
                raise ConcurrencyConflict("approval wait lost node CAS")
            changed = await transaction.execute(
                """
                UPDATE workflow_runs SET status = 'waiting_approval'
                WHERE id = ? AND status = 'running' AND cancel_requested_at IS NULL
                """,
                (str(current["workflow_run_id"]),),
            )
            if changed != 1:
                raise ConcurrencyConflict("approval wait lost workflow CAS")
            await self._append_node_state(
                transaction,
                current,
                lease,
                node_run_id=node_run_id_value,
                node_id=str(current["node_id"]),
                previous=NodeRunStatus.RUNNING,
                target=NodeRunStatus.WAITING_APPROVAL,
                summary="Waiting for durable approval.",
                error_code=f"approval_pending:{approval_id}",
                now=now,
            )
            await self._append_run_state(
                transaction,
                current,
                lease,
                previous=WorkflowRunStatus.RUNNING,
                target=WorkflowRunStatus.WAITING_APPROVAL,
                now=now,
            )
            row = await transaction.fetch_one(
                "SELECT * FROM node_runs WHERE id = ?", (node_run_id_value,)
            )
        assert row is not None
        return _node_record(row)

    async def resume_after_approval(
        self,
        workflow_run_id: str,
        node_run_id_value: str,
        *,
        approved: bool,
        lease: MasterLease,
        now: datetime | None = None,
    ) -> NodeRunRecord:
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            current = await transaction.fetch_one(
                """
                SELECT nr.*, wr.workflow_id, wr.session_id, wr.status AS run_status,
                       wr.cancel_requested_at
                FROM node_runs nr JOIN workflow_runs wr ON nr.workflow_run_id = wr.id
                WHERE nr.id = ? AND nr.workflow_run_id = ?
                """,
                (node_run_id_value, workflow_run_id),
            )
            if current is None:
                raise RecordNotFound(f"node run not found: {node_run_id_value}")
            if (
                NodeRunStatus(str(current["status"])) != NodeRunStatus.WAITING_APPROVAL
                or WorkflowRunStatus(str(current["run_status"]))
                != WorkflowRunStatus.WAITING_APPROVAL
            ):
                raise ConcurrencyConflict("approval resume requires a waiting workflow")
            if current["cancel_requested_at"] is not None:
                raise ConcurrencyConflict("cancellation prevents approval resume")
            target = NodeRunStatus.READY if approved else NodeRunStatus.COMPLETED
            outcome = None if approved else NodeOutcome.REJECTED
            changed = await transaction.execute(
                """
                UPDATE node_runs SET status = ?, outcome = ?, error_code = ?,
                    finished_at = ?
                WHERE id = ? AND status = 'waiting_approval'
                """,
                (
                    target.value,
                    outcome.value if outcome is not None else None,
                    None if approved else "approval_rejected",
                    None if approved else timestamp,
                    node_run_id_value,
                ),
            )
            if changed != 1:
                raise ConcurrencyConflict("approval resume lost node CAS")
            changed = await transaction.execute(
                """
                UPDATE workflow_runs SET status = 'running'
                WHERE id = ? AND status = 'waiting_approval'
                  AND cancel_requested_at IS NULL
                """,
                (workflow_run_id,),
            )
            if changed != 1:
                raise ConcurrencyConflict("approval resume lost workflow CAS")
            await self._append_node_state(
                transaction,
                current,
                lease,
                node_run_id=node_run_id_value,
                node_id=str(current["node_id"]),
                previous=NodeRunStatus.WAITING_APPROVAL,
                target=target,
                outcome=outcome,
                summary="Approval granted." if approved else "Approval rejected.",
                error_code=None if approved else "approval_rejected",
                now=now,
            )
            await self._append_run_state(
                transaction,
                current,
                lease,
                previous=WorkflowRunStatus.WAITING_APPROVAL,
                target=WorkflowRunStatus.RUNNING,
                now=now,
            )
            if not approved:
                fresh_run = await _run_row(transaction, workflow_run_id)
                await self._reconcile_in(transaction, fresh_run, lease, now=now)
            row = await transaction.fetch_one(
                "SELECT * FROM node_runs WHERE id = ?", (node_run_id_value,)
            )
        assert row is not None
        return _node_record(row)

    async def resume_after_approval_in(
        self,
        transaction: Transaction,
        workflow_run_id: str,
        node_run_id_value: str,
        *,
        approved: bool,
        lease: MasterLease,
        now: datetime | None = None,
    ) -> NodeRunRecord:
        """Advance an approval gate inside the caller's durable transaction."""

        timestamp = utc_now_text(now)
        current = await transaction.fetch_one(
            """
            SELECT nr.*, wr.workflow_id, wr.session_id, wr.status AS run_status,
                   wr.cancel_requested_at
            FROM node_runs nr JOIN workflow_runs wr ON nr.workflow_run_id = wr.id
            WHERE nr.id = ? AND nr.workflow_run_id = ?
            """,
            (node_run_id_value, workflow_run_id),
        )
        if current is None:
            raise RecordNotFound(f"node run not found: {node_run_id_value}")
        node_status = NodeRunStatus(str(current["status"]))
        run_status = WorkflowRunStatus(str(current["run_status"]))
        if (
            node_status == NodeRunStatus.WAITING_APPROVAL
            and run_status == WorkflowRunStatus.WAITING_APPROVAL
        ):
            if current["cancel_requested_at"] is not None:
                raise ConcurrencyConflict("cancellation prevents approval resume")
            target = NodeRunStatus.READY if approved else NodeRunStatus.COMPLETED
            outcome = None if approved else NodeOutcome.REJECTED
            changed = await transaction.execute(
                """
                UPDATE node_runs SET status = ?, outcome = ?, error_code = ?,
                    finished_at = ?
                WHERE id = ? AND status = 'waiting_approval'
                """,
                (
                    target.value,
                    outcome.value if outcome is not None else None,
                    None if approved else "approval_rejected",
                    None if approved else timestamp,
                    node_run_id_value,
                ),
            )
            if changed != 1:
                raise ConcurrencyConflict("approval resume lost node CAS")
            changed = await transaction.execute(
                """
                UPDATE workflow_runs SET status = 'running'
                WHERE id = ? AND status = 'waiting_approval'
                  AND cancel_requested_at IS NULL
                """,
                (workflow_run_id,),
            )
            if changed != 1:
                raise ConcurrencyConflict("approval resume lost workflow CAS")
            await self._append_node_state(
                transaction,
                current,
                lease,
                node_run_id=node_run_id_value,
                node_id=str(current["node_id"]),
                previous=NodeRunStatus.WAITING_APPROVAL,
                target=target,
                outcome=outcome,
                summary="Approval granted." if approved else "Approval rejected.",
                error_code=None if approved else "approval_rejected",
                now=now,
            )
            await self._append_run_state(
                transaction,
                current,
                lease,
                previous=WorkflowRunStatus.WAITING_APPROVAL,
                target=WorkflowRunStatus.RUNNING,
                now=now,
            )
            if not approved:
                fresh_run = await _run_row(transaction, workflow_run_id)
                await self._reconcile_in(transaction, fresh_run, lease, now=now)
        elif node_status == NodeRunStatus.RUNNING and run_status == WorkflowRunStatus.RUNNING:
            if approved:
                row = await transaction.fetch_one(
                    "SELECT * FROM node_runs WHERE id = ?", (node_run_id_value,)
                )
                assert row is not None
                return _node_record(row)
            if current["cancel_requested_at"] is not None:
                raise ConcurrencyConflict("cancellation prevents approval rejection")
            changed = await transaction.execute(
                """
                UPDATE node_runs SET status = 'completed', outcome = 'rejected',
                    error_code = 'approval_rejected', finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (timestamp, node_run_id_value),
            )
            if changed != 1:
                raise ConcurrencyConflict("approval rejection lost node CAS")
            await self._append_node_state(
                transaction,
                current,
                lease,
                node_run_id=node_run_id_value,
                node_id=str(current["node_id"]),
                previous=NodeRunStatus.RUNNING,
                target=NodeRunStatus.COMPLETED,
                outcome=NodeOutcome.REJECTED,
                summary="Approval rejected.",
                error_code="approval_rejected",
                now=now,
            )
            fresh_run = await _run_row(transaction, workflow_run_id)
            await self._reconcile_in(transaction, fresh_run, lease, now=now)
        else:
            raise ConcurrencyConflict("approval resume requires a waiting workflow")
        row = await transaction.fetch_one(
            "SELECT * FROM node_runs WHERE id = ?", (node_run_id_value,)
        )
        assert row is not None
        return _node_record(row)

    async def begin_merge_finalizing(
        self,
        workflow_run_id: str,
        *,
        workspace_lease: WorkspaceLease,
        lease: MasterLease,
        now: datetime | None = None,
    ) -> WorkflowRunRecord:
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            row = await _run_row(transaction, workflow_run_id)
            expected_resource = f"session:{row['session_id']}:integration"
            if (
                workspace_lease.resource_key != expected_resource
                or workspace_lease.owner_kind != "merge"
            ):
                raise ValueError("merge finalization requires the session merge workspace lease")
            merge_node = await transaction.fetch_one(
                """
                SELECT id FROM node_runs
                WHERE workflow_run_id = ? AND node_type = 'merge_patch'
                  AND status = 'running'
                ORDER BY attempt DESC LIMIT 1
                """,
                (workflow_run_id,),
            )
            if merge_node is None or workspace_lease.owner_operation_id != str(merge_node["id"]):
                raise ValueError("merge finalization lease is not bound to the running merge node")
            await self._workspace_leases.assert_valid_in(transaction, workspace_lease, now=now)
            if WorkflowRunStatus(str(row["status"])) != WorkflowRunStatus.RUNNING:
                raise ConcurrencyConflict("merge finalization requires a running workflow")
            if row["cancel_requested_at"] is not None:
                raise ConcurrencyConflict("cancel requested before merge finalization")
            changed = await transaction.execute(
                """
                UPDATE workflow_runs SET merge_finalizing_at = ?
                WHERE id = ? AND status = 'running'
                  AND cancel_requested_at IS NULL AND merge_finalizing_at IS NULL
                """,
                (timestamp, workflow_run_id),
            )
            if changed != 1:
                raise ConcurrencyConflict("merge finalization CAS lost")
            row = await _run_row(transaction, workflow_run_id)
        return _run_record(row)

    async def resume_privilege_after_approval_in(
        self,
        transaction: Transaction,
        workflow_run_id: str,
        node_run_id_value: str,
        *,
        approved: bool,
        lease: MasterLease,
        now: datetime | None = None,
    ) -> None:
        current = await transaction.fetch_one(
            """
            SELECT nr.*, wr.workflow_id, wr.session_id, wr.status AS run_status,
                   wr.cancel_requested_at, t.id AS source_task_id,
                   t.status AS source_task_status
            FROM node_runs nr JOIN workflow_runs wr ON wr.id = nr.workflow_run_id
            JOIN tasks t ON t.node_run_id = nr.id
            WHERE nr.id = ? AND nr.workflow_run_id = ?
            """,
            (node_run_id_value, workflow_run_id),
        )
        if current is None:
            raise RecordNotFound(f"privilege node not found: {node_run_id_value}")
        if (
            NodeRunStatus(str(current["status"])) != NodeRunStatus.WAITING_APPROVAL
            or WorkflowRunStatus(str(current["run_status"])) != WorkflowRunStatus.WAITING_APPROVAL
            or current["cancel_requested_at"] is not None
        ):
            raise ConcurrencyConflict("privilege approval resume requires a waiting workflow")
        target = await transaction.fetch_one(
            """
            SELECT nr.*, t.id AS task_id
            FROM node_runs nr JOIN tasks t ON t.node_run_id = nr.id
            WHERE nr.workflow_run_id = ? AND nr.node_id = ? AND nr.attempt > ?
            ORDER BY nr.attempt ASC LIMIT 1
            """,
            (workflow_run_id, str(current["node_id"]), int(current["attempt"])),
        )
        if target is None:
            raise ConcurrencyConflict("privilege retry target is missing")
        timestamp = utc_now_text(now)
        if approved:
            changed = await transaction.execute(
                """
                UPDATE node_runs SET status = 'superseded', outcome = 'cancelled',
                    error_code = 'privilege_retry_approved', finished_at = ?
                WHERE id = ? AND status = 'waiting_approval'
                """,
                (timestamp, node_run_id_value),
            )
            if changed != 1:
                raise ConcurrencyConflict("privilege approval supersede lost CAS")
            await self._append_node_state(
                transaction,
                current,
                lease,
                node_run_id=node_run_id_value,
                node_id=str(current["node_id"]),
                previous=NodeRunStatus.WAITING_APPROVAL,
                target=NodeRunStatus.SUPERSEDED,
                outcome=NodeOutcome.CANCELLED,
                summary="Privilege approval created a new task attempt.",
                error_code="privilege_retry_approved",
                now=now,
            )
            return
        changed = await transaction.execute(
            """
            UPDATE tasks SET status = 'failed', error_code = 'privilege_rejected',
                finished_at = ?
            WHERE id = ? AND status = 'privilege_requested'
            """,
            (timestamp, str(current["source_task_id"])),
        )
        if changed != 1:
            raise ConcurrencyConflict("privilege rejection lost source task CAS")
        await self._append_task_state(
            transaction,
            current,
            lease,
            task_id=str(current["source_task_id"]),
            previous=TaskStatus.PRIVILEGE_REQUESTED,
            target=TaskStatus.FAILED,
            error_code="privilege_rejected",
            now=now,
        )
        changed = await transaction.execute(
            """
            UPDATE node_runs SET status = 'completed', outcome = 'rejected',
                error_code = 'privilege_rejected', finished_at = ?
            WHERE id = ? AND status = 'waiting_approval'
            """,
            (timestamp, node_run_id_value),
        )
        if changed != 1:
            raise ConcurrencyConflict("privilege rejection lost source node CAS")
        await transaction.execute(
            "UPDATE tasks SET status = 'cancelled', finished_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (timestamp, str(target["task_id"])),
        )
        changed = await transaction.execute(
            """
            UPDATE node_runs SET status = 'skipped', outcome = NULL,
                error_code = 'privilege_rejected', finished_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (timestamp, str(target["id"])),
        )
        if changed != 1:
            raise ConcurrencyConflict("privilege rejection lost retry target CAS")
        await self._append_node_state(
            transaction,
            current,
            lease,
            node_run_id=node_run_id_value,
            node_id=str(current["node_id"]),
            previous=NodeRunStatus.WAITING_APPROVAL,
            target=NodeRunStatus.COMPLETED,
            outcome=NodeOutcome.REJECTED,
            summary="Privilege request was rejected.",
            error_code="privilege_rejected",
            now=now,
        )
        await self._append_node_state(
            transaction,
            current,
            lease,
            node_run_id=str(target["id"]),
            node_id=str(target["node_id"]),
            previous=NodeRunStatus.PENDING,
            target=NodeRunStatus.SKIPPED,
            summary="Privilege retry was rejected.",
            error_code="privilege_rejected",
            now=now,
        )
        changed = await transaction.execute(
            "UPDATE workflow_runs SET status = 'running' "
            "WHERE id = ? AND status = 'waiting_approval' AND cancel_requested_at IS NULL",
            (workflow_run_id,),
        )
        if changed != 1:
            raise ConcurrencyConflict("privilege rejection lost workflow CAS")
        await self._append_run_state(
            transaction,
            current,
            lease,
            previous=WorkflowRunStatus.WAITING_APPROVAL,
            target=WorkflowRunStatus.RUNNING,
            now=now,
        )
        await self._reconcile_in(
            transaction,
            await _run_row(transaction, workflow_run_id),
            lease,
            now=now,
        )

    async def activate_capability_target_in(
        self,
        transaction: Transaction,
        *,
        target_task_id: str,
        lease: MasterLease,
        now: datetime | None = None,
    ) -> None:
        current = await transaction.fetch_one(
            """
            SELECT nr.*, wr.workflow_id, wr.session_id, wr.status AS run_status,
                   wr.cancel_requested_at, t.status AS task_status
            FROM tasks t
            JOIN node_runs nr ON nr.id = t.node_run_id
            JOIN workflow_runs wr ON wr.id = nr.workflow_run_id
            WHERE t.id = ?
            """,
            (target_task_id,),
        )
        if current is None:
            raise RecordNotFound(f"capability target task not found: {target_task_id}")
        if NodeRunStatus(str(current["status"])) == NodeRunStatus.READY:
            return
        if (
            NodeRunStatus(str(current["status"])) != NodeRunStatus.PENDING
            or TaskStatus(str(current["task_status"])) != TaskStatus.PENDING
            or current["cancel_requested_at"] is not None
        ):
            raise ConcurrencyConflict("capability target is not a pending retry")
        changed = await transaction.execute(
            "UPDATE node_runs SET status = 'ready', error_code = NULL "
            "WHERE id = ? AND status = 'pending'",
            (str(current["id"]),),
        )
        if changed != 1:
            raise ConcurrencyConflict("capability target ready CAS lost")
        await self._append_node_state(
            transaction,
            current,
            lease,
            node_run_id=str(current["id"]),
            node_id=str(current["node_id"]),
            previous=NodeRunStatus.PENDING,
            target=NodeRunStatus.READY,
            summary="Capability grant activated the retry task.",
            now=now,
        )
        changed = await transaction.execute(
            """
            UPDATE workflow_runs SET status = 'running'
            WHERE id = ? AND status = 'waiting_approval' AND cancel_requested_at IS NULL
            """,
            (str(current["workflow_run_id"]),),
        )
        if changed == 1:
            await self._append_run_state(
                transaction,
                current,
                lease,
                previous=WorkflowRunStatus.WAITING_APPROVAL,
                target=WorkflowRunStatus.RUNNING,
                now=now,
            )

    async def clear_merge_finalizing(
        self,
        workflow_run_id: str,
        *,
        workspace_lease: WorkspaceLease,
        lease: MasterLease,
        now: datetime | None = None,
    ) -> WorkflowRunRecord:
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            row = await _run_row(transaction, workflow_run_id)
            expected_resource = f"session:{row['session_id']}:integration"
            if workspace_lease.resource_key != expected_resource:
                raise ValueError("merge finalization clear requires the session workspace lease")
            merge_node = await transaction.fetch_one(
                """
                SELECT id FROM node_runs
                WHERE workflow_run_id = ? AND node_type = 'merge_patch'
                  AND status = 'running'
                ORDER BY attempt DESC LIMIT 1
                """,
                (workflow_run_id,),
            )
            expected_operations = {f"recovery-{workflow_run_id}"}
            if merge_node is not None:
                expected_operations.add(str(merge_node["id"]))
            if workspace_lease.owner_kind not in {"merge", "recovery"} or (
                workspace_lease.owner_operation_id not in expected_operations
            ):
                raise ValueError("merge finalization clear lease is not bound to this workflow")
            await self._workspace_leases.assert_valid_in(transaction, workspace_lease, now=now)
            changed = await transaction.execute(
                """
                UPDATE workflow_runs SET merge_finalizing_at = NULL
                WHERE id = ? AND merge_finalizing_at IS NOT NULL
                """,
                (workflow_run_id,),
            )
            if changed not in {0, 1}:
                raise ConcurrencyConflict("merge finalization clear failed")
            row = await _run_row(transaction, workflow_run_id)
        return _run_record(row)

    async def finalize_merge(
        self,
        node_run_id_value: str,
        *,
        change_set_id: str,
        new_commit: str,
        workspace_lease: WorkspaceLease,
        lease: MasterLease,
        summary: str = "Master committed the approved ChangeSet.",
        now: datetime | None = None,
    ) -> NodeRunRecord:
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            await self._workspace_leases.assert_valid_in(transaction, workspace_lease, now=now)
            current = await transaction.fetch_one(
                """
                SELECT nr.*, wr.workflow_id, wr.session_id, wr.status AS run_status,
                       wr.current_commit, wr.cancel_requested_at,
                       wr.merge_finalizing_at
                FROM node_runs nr JOIN workflow_runs wr ON nr.workflow_run_id = wr.id
                WHERE nr.id = ?
                """,
                (node_run_id_value,),
            )
            if current is None:
                raise RecordNotFound(f"node run not found: {node_run_id_value}")
            if (
                NodeRunStatus(str(current["status"])) != NodeRunStatus.RUNNING
                or WorkflowRunStatus(str(current["run_status"])) != WorkflowRunStatus.RUNNING
                or current["merge_finalizing_at"] is None
                or current["cancel_requested_at"] is not None
            ):
                raise ConcurrencyConflict("merge finalization state is no longer writable")
            expected_resource = f"session:{current['session_id']}:integration"
            if workspace_lease.resource_key != expected_resource:
                raise ValueError("merge workspace lease has the wrong resource")
            expected_operation = (
                str(current["id"])
                if workspace_lease.owner_kind == "merge"
                else f"recovery-{current['workflow_run_id']}"
            )
            if workspace_lease.owner_kind not in {"merge", "recovery"} or (
                workspace_lease.owner_operation_id != expected_operation
            ):
                raise ValueError("merge workspace lease is not bound to this finalization")
            session = await transaction.fetch_one(
                "SELECT integration_head_commit FROM sessions WHERE id = ?",
                (str(current["session_id"]),),
            )
            if session is None or str(session["integration_head_commit"]) != str(
                current["current_commit"]
            ):
                raise ConcurrencyConflict("session integration HEAD changed before commit")
            changed = await transaction.execute(
                """
                UPDATE sessions SET integration_head_commit = ?, updated_at = ?
                WHERE id = ? AND integration_head_commit = ?
                """,
                (
                    new_commit,
                    timestamp,
                    str(current["session_id"]),
                    str(current["current_commit"]),
                ),
            )
            if changed != 1:
                raise ConcurrencyConflict("session integration HEAD CAS failed")
            changed = await transaction.execute(
                """
                UPDATE workflow_runs SET current_commit = ?, merge_finalizing_at = NULL
                WHERE id = ? AND status = 'running' AND merge_finalizing_at IS NOT NULL
                """,
                (new_commit, str(current["workflow_run_id"])),
            )
            if changed != 1:
                raise ConcurrencyConflict("workflow merge finalization CAS failed")
            changed = await transaction.execute(
                """
                UPDATE change_sets SET status = 'merged', updated_at = ?
                WHERE id = ? AND status = 'approved'
                """,
                (timestamp, change_set_id),
            )
            if changed != 1:
                raise ConcurrencyConflict("ChangeSet is not approved for merge")
            changed = await transaction.execute(
                """
                UPDATE node_runs SET status = 'completed', outcome = 'success',
                    error_code = NULL, finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (timestamp, node_run_id_value),
            )
            if changed != 1:
                raise ConcurrencyConflict("merge node completion CAS failed")
            await self._append_node_state(
                transaction,
                current,
                lease,
                node_run_id=node_run_id_value,
                node_id=str(current["node_id"]),
                previous=NodeRunStatus.RUNNING,
                target=NodeRunStatus.COMPLETED,
                outcome=NodeOutcome.SUCCESS,
                summary=summary,
                now=now,
            )
            fresh_run = await _run_row(transaction, str(current["workflow_run_id"]))
            await self._reconcile_in(transaction, fresh_run, lease, now=now)
            row = await transaction.fetch_one(
                "SELECT * FROM node_runs WHERE id = ?", (node_run_id_value,)
            )
        assert row is not None
        return _node_record(row)

    async def attach_completed_node_artifact(
        self,
        node_run_id_value: str,
        *,
        artifact_id: str,
        lease: MasterLease,
        now: datetime | None = None,
    ) -> NodeRunRecord:
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            row = await transaction.fetch_one(
                "SELECT * FROM node_runs WHERE id = ?",
                (node_run_id_value,),
            )
            if row is None:
                raise RecordNotFound(f"node run not found: {node_run_id_value}")
            if NodeRunStatus(str(row["status"])) != NodeRunStatus.COMPLETED:
                raise ConcurrencyConflict("only a completed node can receive a result artifact")
            existing = row["output_artifact_id"]
            if existing is not None and str(existing) != artifact_id:
                raise ConcurrencyConflict("completed node already has another result artifact")
            if existing is None:
                changed = await transaction.execute(
                    "UPDATE node_runs SET output_artifact_id = ? "
                    "WHERE id = ? AND status = 'completed' AND output_artifact_id IS NULL",
                    (artifact_id, node_run_id_value),
                )
                if changed != 1:
                    raise ConcurrencyConflict("completed node artifact CAS lost")
                row = await transaction.fetch_one(
                    "SELECT * FROM node_runs WHERE id = ?",
                    (node_run_id_value,),
                )
        assert row is not None
        return _node_record(row)

    async def request_cancel(
        self,
        workflow_run_id: str,
        *,
        lease: MasterLease,
        now: datetime | None = None,
    ) -> WorkflowRunRecord:
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            row = await _run_row(transaction, workflow_run_id)
            status = WorkflowRunStatus(str(row["status"]))
            if status in _RUN_TERMINAL:
                return _run_record(row)
            if row["merge_finalizing_at"] is not None:
                raise ConcurrencyConflict(
                    "merge finalization is the cancellation linearization point"
                )
            if row["cancel_requested_at"] is not None:
                return _run_record(row)
            changed = await transaction.execute(
                """
                UPDATE workflow_runs SET cancel_requested_at = ?
                WHERE id = ? AND cancel_requested_at IS NULL
                  AND merge_finalizing_at IS NULL
                """,
                (timestamp, workflow_run_id),
            )
            if changed != 1:
                raise ConcurrencyConflict("cancel request CAS lost")
        return await self.get(workflow_run_id)

    async def list_recoverable(self) -> list[WorkflowRunRecord]:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM workflow_runs
                WHERE status IN ('running', 'waiting_approval', 'orphaned')
                   OR merge_finalizing_at IS NOT NULL
                ORDER BY created_at, id
                """
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [_run_record(row) for row in rows]

    async def complete_cancel(
        self,
        workflow_run_id: str,
        *,
        lease: MasterLease,
        now: datetime | None = None,
    ) -> WorkflowRunRecord:
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            run = await _run_row(transaction, workflow_run_id)
            previous_run = WorkflowRunStatus(str(run["status"]))
            if previous_run in _RUN_TERMINAL:
                return _run_record(run)
            if run["merge_finalizing_at"] is not None:
                raise ConcurrencyConflict("cannot complete cancellation during merge finalization")
            rows = await transaction.fetch_all(
                """
                SELECT nr.*, wr.workflow_id, wr.session_id, wr.status AS run_status
                FROM node_runs nr JOIN workflow_runs wr ON nr.workflow_run_id = wr.id
                WHERE nr.workflow_run_id = ?
                  AND nr.status IN ('pending', 'ready', 'running', 'waiting_approval')
                """,
                (workflow_run_id,),
            )
            for current in rows:
                previous = NodeRunStatus(str(current["status"]))
                changed = await transaction.execute(
                    """
                    UPDATE node_runs SET status = 'cancelled', outcome = 'cancelled',
                        error_code = 'workflow_cancelled', finished_at = ?
                    WHERE id = ? AND status IN ('pending', 'ready', 'running', 'waiting_approval')
                    """,
                    (timestamp, str(current["id"])),
                )
                if changed != 1:
                    raise ConcurrencyConflict("node cancellation CAS lost")
                await transaction.execute(
                    """
                    UPDATE tasks SET status = 'cancelled', finished_at = ?
                    WHERE node_run_id = ? AND status IN (
                        'pending', 'running', 'privilege_requested'
                    )
                    """,
                    (timestamp, str(current["id"])),
                )
                await self._append_node_state(
                    transaction,
                    current,
                    lease,
                    node_run_id=str(current["id"]),
                    node_id=str(current["node_id"]),
                    previous=previous,
                    target=NodeRunStatus.CANCELLED,
                    outcome=NodeOutcome.CANCELLED,
                    summary="Workflow cancellation completed.",
                    error_code="workflow_cancelled",
                    now=now,
                )
            changed = await transaction.execute(
                """
                UPDATE workflow_runs SET status = 'cancelled', finished_at = ?
                WHERE id = ? AND status NOT IN (
                    'blocked', 'failed', 'completed', 'cancelled', 'orphaned'
                )
                  AND merge_finalizing_at IS NULL
                """,
                (timestamp, workflow_run_id),
            )
            if changed != 1:
                raise ConcurrencyConflict("workflow cancellation CAS lost")
            await self._append_run_state(
                transaction,
                run,
                lease,
                previous=previous_run,
                target=WorkflowRunStatus.CANCELLED,
                now=now,
            )
            run = await _run_row(transaction, workflow_run_id)
        return _run_record(run)

    async def cancel_with_cleanup(
        self,
        workflow_run_id: str,
        *,
        approvals: object,
        capabilities: object,
        lease: MasterLease,
        reason: str = "workflow_cancelled",
        now: datetime | None = None,
    ) -> WorkflowRunRecord:
        """Linearize cancellation, revocations, node state, and audit in one tx."""

        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            run = await _run_row(transaction, workflow_run_id)
            previous = WorkflowRunStatus(str(run["status"]))
            if previous in _RUN_TERMINAL and run["cancel_requested_at"] is None:
                return _run_record(run)
            if run["merge_finalizing_at"] is not None:
                raise ConcurrencyConflict(
                    "merge finalization is the cancellation linearization point"
                )
            if run["cancel_requested_at"] is None:
                changed = await transaction.execute(
                    """
                    UPDATE workflow_runs SET cancel_requested_at = ?
                    WHERE id = ? AND cancel_requested_at IS NULL
                      AND merge_finalizing_at IS NULL
                    """,
                    (timestamp, workflow_run_id),
                )
                if changed != 1:
                    raise ConcurrencyConflict("cancel request CAS lost")
                run = await _run_row(transaction, workflow_run_id)
            invalidate = getattr(approvals, "invalidate_for_run_in", None)
            revoke = getattr(capabilities, "revoke_for_run_in", None)
            if invalidate is None or revoke is None:
                raise RuntimeError("cancellation dependencies lack atomic transaction hooks")
            await invalidate(
                transaction,
                workflow_run_id=workflow_run_id,
                master_lease=lease,
                reason=reason,
                now=now,
            )
            await revoke(
                transaction,
                workflow_run_id,
                master_lease=lease,
                reason=reason,
                now=now,
            )
            result = await self._complete_cancel_in(
                transaction,
                workflow_run_id,
                lease=lease,
                now=now,
            )
            payload = RecoveryEventPayload(
                master_fencing_token=lease.fencing_token,
                workflow_run_id=workflow_run_id,
                session_id=str(run["session_id"]),
                action="cancel",
                outcome="cancelled",
                reason="cancellation and all dependent authorization state committed atomically",
            )
            await self._events.append_in(
                transaction,
                session_id=str(run["session_id"]),
                workflow_id=str(run["workflow_id"]),
                workflow_run_id=workflow_run_id,
                event_type=RECOVERY_ACTION,
                actor_type=ActorType.MASTER,
                actor_id=lease.instance_id,
                payload=payload,
                now=now,
            )
            await self._events.append_security_event_in(
                transaction,
                session_id=str(run["session_id"]),
                workflow_run_id=workflow_run_id,
                task_id=None,
                event_type="workflow.cancellation_completed",
                severity="info",
                payload=payload,
                now=now,
            )
            return result

    async def _complete_cancel_in(
        self,
        transaction: Transaction,
        workflow_run_id: str,
        *,
        lease: MasterLease,
        now: datetime | None = None,
    ) -> WorkflowRunRecord:
        timestamp = utc_now_text(now)
        run = await _run_row(transaction, workflow_run_id)
        previous_run = WorkflowRunStatus(str(run["status"]))
        if previous_run in _RUN_TERMINAL:
            return _run_record(run)
        if run["merge_finalizing_at"] is not None:
            raise ConcurrencyConflict("cannot complete cancellation during merge finalization")
        rows = await transaction.fetch_all(
            """
            SELECT nr.*, wr.workflow_id, wr.session_id, wr.status AS run_status
            FROM node_runs nr JOIN workflow_runs wr ON nr.workflow_run_id = wr.id
            WHERE nr.workflow_run_id = ?
              AND nr.status IN ('pending', 'ready', 'running', 'waiting_approval')
            """,
            (workflow_run_id,),
        )
        for current in rows:
            previous = NodeRunStatus(str(current["status"]))
            changed = await transaction.execute(
                """
                UPDATE node_runs SET status = 'cancelled', outcome = 'cancelled',
                    error_code = 'workflow_cancelled', finished_at = ?
                WHERE id = ? AND status IN ('pending', 'ready', 'running', 'waiting_approval')
                """,
                (timestamp, str(current["id"])),
            )
            if changed != 1:
                raise ConcurrencyConflict("node cancellation CAS lost")
            await transaction.execute(
                """
                UPDATE tasks SET status = 'cancelled', finished_at = ?
                WHERE node_run_id = ? AND status IN ('pending', 'running', 'privilege_requested')
                """,
                (timestamp, str(current["id"])),
            )
            await self._append_node_state(
                transaction,
                current,
                lease,
                node_run_id=str(current["id"]),
                node_id=str(current["node_id"]),
                previous=previous,
                target=NodeRunStatus.CANCELLED,
                outcome=NodeOutcome.CANCELLED,
                summary="Workflow cancellation completed.",
                error_code="workflow_cancelled",
                now=now,
            )
        changed = await transaction.execute(
            """
            UPDATE workflow_runs SET status = 'cancelled', finished_at = ?
            WHERE id = ? AND status NOT IN (
                'blocked', 'failed', 'completed', 'cancelled', 'orphaned'
            )
              AND merge_finalizing_at IS NULL
            """,
            (timestamp, workflow_run_id),
        )
        if changed != 1:
            raise ConcurrencyConflict("workflow cancellation CAS lost")
        await self._append_run_state(
            transaction,
            run,
            lease,
            previous=previous_run,
            target=WorkflowRunStatus.CANCELLED,
            now=now,
        )
        return _run_record(await _run_row(transaction, workflow_run_id))

    async def mark_orphaned(
        self,
        workflow_run_id: str,
        *,
        lease: MasterLease,
        reason: str,
        node_run_id_value: str | None = None,
        now: datetime | None = None,
    ) -> WorkflowRunRecord:
        async with self._database.immediate_transaction() as transaction:
            await self._leases.assert_valid_in(transaction, lease, now=now)
            run = await _run_row(transaction, workflow_run_id)
            previous_run = WorkflowRunStatus(str(run["status"]))
            if previous_run in _RUN_TERMINAL and previous_run != WorkflowRunStatus.ORPHANED:
                return _run_record(run)
            rows = await transaction.fetch_all(
                """
                SELECT nr.*, wr.workflow_id, wr.session_id, wr.status AS run_status
                FROM node_runs nr JOIN workflow_runs wr ON nr.workflow_run_id = wr.id
                WHERE nr.workflow_run_id = ? AND nr.status IN ('running', 'waiting_approval')
                """,
                (workflow_run_id,),
            )
            for current in rows:
                if node_run_id_value is not None and str(current["id"]) != node_run_id_value:
                    continue
                previous = NodeRunStatus(str(current["status"]))
                await transaction.execute(
                    """
                    UPDATE node_runs SET status = 'orphaned',
                        error_code = ?, finished_at = COALESCE(finished_at, ?)
                    WHERE id = ? AND status IN ('running', 'waiting_approval')
                    """,
                    (reason[:200], utc_now_text(now), str(current["id"])),
                )
                await self._append_node_state(
                    transaction,
                    current,
                    lease,
                    node_run_id=str(current["id"]),
                    node_id=str(current["node_id"]),
                    previous=previous,
                    target=NodeRunStatus.ORPHANED,
                    summary="Recovery quarantined an uncertain node.",
                    error_code=reason[:200],
                    now=now,
                )
            if previous_run != WorkflowRunStatus.ORPHANED:
                await transaction.execute(
                    """
                    UPDATE workflow_runs SET status = 'orphaned',
                        finished_at = COALESCE(finished_at, ?)
                    WHERE id = ? AND status IN ('pending', 'running', 'waiting_approval', 'paused')
                    """,
                    (utc_now_text(now), workflow_run_id),
                )
                await transaction.execute(
                    "UPDATE sessions SET status = 'blocked', updated_at = ? WHERE id = ?",
                    (utc_now_text(now), str(run["session_id"])),
                )
                await self._append_run_state(
                    transaction,
                    run,
                    lease,
                    previous=previous_run,
                    target=WorkflowRunStatus.ORPHANED,
                    now=now,
                )
            run = await _run_row(transaction, workflow_run_id)
        return _run_record(run)

    async def _reconcile_in(
        self,
        transaction: Transaction,
        run: aiosqlite.Row,
        lease: MasterLease,
        *,
        now: datetime | None,
    ) -> None:
        graph = CompiledGraph.model_validate_json(str(run["compiled_snapshot_json"]))
        incoming: dict[str, list[object]] = defaultdict(list)
        for edge in graph.edges:
            incoming[edge.to_node].append(edge)

        while True:
            rows = await transaction.fetch_all(
                "SELECT * FROM node_runs WHERE workflow_run_id = ?",
                (str(run["id"]),),
            )
            by_node = {str(row["node_id"]): row for row in rows}
            changed_any = False
            for node_id in _topological_order(graph):
                row = by_node[node_id]
                if NodeRunStatus(str(row["status"])) != NodeRunStatus.PENDING:
                    continue
                node = next(item for item in graph.nodes if item.id == node_id)
                if node.node_type == NodeType.INPUT:
                    target = NodeRunStatus.READY
                else:
                    edges = incoming.get(node_id, [])
                    resolved = True
                    satisfied = False
                    for edge in edges:
                        upstream = by_node[edge.from_node]
                        upstream_status = NodeRunStatus(str(upstream["status"]))
                        if upstream_status not in _NODE_TERMINAL:
                            resolved = False
                            break
                        upstream_outcome = (
                            NodeOutcome(str(upstream["outcome"]))
                            if upstream["outcome"] is not None
                            else None
                        )
                        if _edge_satisfied(edge.condition, upstream_status, upstream_outcome):
                            satisfied = True
                    if not resolved:
                        continue
                    target = NodeRunStatus.READY if satisfied else NodeRunStatus.SKIPPED

                finished_at = utc_now_text(now) if target == NodeRunStatus.SKIPPED else None
                changed = await transaction.execute(
                    """
                    UPDATE node_runs SET status = ?, finished_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (target.value, finished_at, str(row["id"])),
                )
                if changed != 1:
                    raise ConcurrencyConflict("pending node reconciliation lost CAS")
                await self._append_node_state(
                    transaction,
                    run,
                    lease,
                    node_run_id=str(row["id"]),
                    node_id=node_id,
                    previous=NodeRunStatus.PENDING,
                    target=target,
                    summary="inactive branch" if target == NodeRunStatus.SKIPPED else "ready",
                    now=now,
                )
                changed_any = True
            if not changed_any:
                break

        rows = await transaction.fetch_all(
            "SELECT * FROM node_runs WHERE workflow_run_id = ?",
            (str(run["id"]),),
        )
        statuses = [NodeRunStatus(str(row["status"])) for row in rows]
        if all(status in _NODE_TERMINAL for status in statuses):
            output_id = next(node.id for node in graph.nodes if node.node_type == NodeType.OUTPUT)
            output = next(row for row in rows if str(row["node_id"]) == output_id)
            output_status = NodeRunStatus(str(output["status"]))
            output_outcome = (
                NodeOutcome(str(output["outcome"])) if output["outcome"] is not None else None
            )
            if output_status == NodeRunStatus.COMPLETED and output_outcome in {
                NodeOutcome.SUCCESS,
                NodeOutcome.REJECTED,
            }:
                target = WorkflowRunStatus.COMPLETED
            elif output_status == NodeRunStatus.BLOCKED_BY_GUARD or any(
                status == NodeRunStatus.BLOCKED_BY_GUARD for status in statuses
            ):
                target = WorkflowRunStatus.BLOCKED
            else:
                target = WorkflowRunStatus.FAILED
            await self._finish_run_in(transaction, run, lease, target=target, now=now)

    async def _finish_run_in(
        self,
        transaction: Transaction,
        run: aiosqlite.Row,
        lease: MasterLease,
        *,
        target: WorkflowRunStatus,
        now: datetime | None,
    ) -> None:
        timestamp = utc_now_text(now)
        changed = await transaction.execute(
            """
            UPDATE workflow_runs SET status = ?, finished_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (target.value, timestamp, str(run["id"])),
        )
        if changed != 1:
            raise ConcurrencyConflict("workflow terminal transition lost running CAS")
        await self._append_run_state(
            transaction,
            run,
            lease,
            previous=WorkflowRunStatus.RUNNING,
            target=target,
            now=now,
        )

    async def _append_run_state(
        self,
        transaction: Transaction,
        run: aiosqlite.Row,
        lease: MasterLease,
        *,
        previous: WorkflowRunStatus,
        target: WorkflowRunStatus,
        now: datetime | None,
    ) -> None:
        workflow_run_id = str(
            run["workflow_run_id"] if "workflow_run_id" in tuple(run.keys()) else run["id"]
        )
        await self._events.append_in(
            transaction,
            session_id=str(run["session_id"]),
            workflow_id=str(run["workflow_id"]),
            workflow_run_id=workflow_run_id,
            event_type=RUN_STATE_CHANGED,
            actor_type=ActorType.MASTER,
            actor_id=lease.instance_id,
            payload=WorkflowRunEventPayload(
                master_fencing_token=lease.fencing_token,
                workflow_run_id=workflow_run_id,
                previous_status=previous,
                status=target,
            ),
            now=now,
        )

    async def _append_node_state(
        self,
        transaction: Transaction,
        run: aiosqlite.Row,
        lease: MasterLease,
        *,
        node_run_id: str,
        node_id: str,
        previous: NodeRunStatus,
        target: NodeRunStatus,
        outcome: NodeOutcome | None = None,
        summary: str = "",
        error_code: str | None = None,
        now: datetime | None,
    ) -> None:
        workflow_run_id = str(
            run["workflow_run_id"] if "workflow_run_id" in tuple(run.keys()) else run["id"]
        )
        await self._events.append_in(
            transaction,
            session_id=str(run["session_id"]),
            workflow_id=str(run["workflow_id"]),
            workflow_run_id=workflow_run_id,
            event_type=NODE_STATE_CHANGED,
            actor_type=ActorType.MASTER,
            actor_id=lease.instance_id,
            payload=NodeRunEventPayload(
                master_fencing_token=lease.fencing_token,
                workflow_run_id=workflow_run_id,
                node_run_id=node_run_id,
                node_id=node_id,
                previous_status=previous,
                status=target,
                outcome=outcome,
                summary=summary,
                error_code=error_code,
            ),
            now=now,
        )

    async def _append_task_state(
        self,
        transaction: Transaction,
        row: aiosqlite.Row,
        lease: MasterLease,
        *,
        task_id: str,
        previous: TaskStatus | None,
        target: TaskStatus,
        error_code: str | None = None,
        workspace_fencing_token: int | None = None,
        now: datetime | None,
    ) -> None:
        await self._events.append_in(
            transaction,
            session_id=str(row["session_id"]),
            workflow_id=str(row["workflow_id"]),
            workflow_run_id=str(row["workflow_run_id"]),
            event_type=TASK_STATE_CHANGED,
            actor_type=ActorType.MASTER,
            actor_id=lease.instance_id,
            payload=TaskEventPayload(
                master_fencing_token=lease.fencing_token,
                workspace_fencing_token=workspace_fencing_token,
                workflow_run_id=str(row["workflow_run_id"]),
                node_run_id=str(
                    row["node_run_id"] if "node_run_id" in tuple(row.keys()) else row["id"]
                ),
                task_id=task_id,
                previous_status=previous,
                status=target,
                error_code=error_code,
            ),
            now=now,
        )


async def _run_row(transaction: Transaction, workflow_run_id: str) -> aiosqlite.Row:
    row = await transaction.fetch_one(
        "SELECT * FROM workflow_runs WHERE id = ?",
        (workflow_run_id,),
    )
    if row is None:
        raise RecordNotFound(f"workflow run not found: {workflow_run_id}")
    return row


def _edge_satisfied(
    condition: EdgeCondition,
    upstream_status: NodeRunStatus,
    outcome: NodeOutcome | None,
) -> bool:
    if upstream_status in {NodeRunStatus.SKIPPED, NodeRunStatus.SUPERSEDED}:
        return False
    expected = {
        EdgeCondition.SUCCESS: {NodeOutcome.SUCCESS},
        EdgeCondition.FAILURE: {NodeOutcome.FAILURE, NodeOutcome.BLOCKED},
        EdgeCondition.MATCHED: {NodeOutcome.MATCHED},
        EdgeCondition.NOT_MATCHED: {NodeOutcome.NOT_MATCHED},
        EdgeCondition.APPROVED: {NodeOutcome.APPROVED},
        EdgeCondition.REJECTED: {NodeOutcome.REJECTED},
    }
    return outcome in expected[condition]


def _topological_order(graph: CompiledGraph) -> list[str]:
    indegree = {node.id: 0 for node in graph.nodes}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        indegree[edge.to_node] += 1
        outgoing[edge.from_node].append(edge.to_node)
    queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    result: list[str] = []
    while queue:
        current = queue.popleft()
        result.append(current)
        for target in sorted(outgoing[current]):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if len(result) != len(graph.nodes):
        raise ValueError("compiled snapshot contains a cycle")
    return result


def node_run_id(workflow_run_id: str, node_id: str, attempt: int) -> str:
    digest = sha256(f"{workflow_run_id}\x00{node_id}\x00{attempt}".encode()).hexdigest()[:24]
    return f"nr-{digest}"


def task_id_for_node(node_run_id_value: str) -> str:
    digest = sha256(f"task\x00{node_run_id_value}".encode()).hexdigest()[:24]
    return f"task-{digest}"


def _normalized_layout(layout: WorkflowLayout) -> WorkflowLayout:
    return WorkflowLayout(nodes=sorted(layout.nodes, key=lambda item: item.node_id))


def _validated_snapshot_models(
    row: aiosqlite.Row,
) -> tuple[AuthorGraph, CompiledGraph, WorkflowLayout, AgentCatalog]:
    try:
        author = AuthorGraph.model_validate_json(str(row["author_snapshot_json"]))
        compiled = CompiledGraph.model_validate_json(str(row["compiled_snapshot_json"]))
        layout = WorkflowLayout.model_validate_json(str(row["layout_snapshot_json"]))
        catalog = AgentCatalog.model_validate_json(str(row["agent_catalog_snapshot_json"]))
    except Exception as exc:
        raise SnapshotIntegrityError("workflow run snapshot JSON is invalid") from exc

    expected = {
        "author_snapshot_hash": sha256(canonical_json(author)).hexdigest(),
        "compiled_snapshot_hash": sha256(canonical_json(compiled)).hexdigest(),
        "layout_snapshot_hash": sha256(canonical_json(layout)).hexdigest(),
        "agent_catalog_snapshot_hash": _agent_catalog_hash(catalog),
    }
    for field, actual in expected.items():
        if str(row[field]) != actual:
            raise SnapshotIntegrityError(f"workflow run {field} does not match its JSON")
    if catalog.catalog_hash != expected["agent_catalog_snapshot_hash"]:
        raise SnapshotIntegrityError("agent catalog embedded hash does not match its agents")
    if compiled.source_author_hash != expected["author_snapshot_hash"]:
        raise SnapshotIntegrityError("compiled snapshot does not match author snapshot")
    if compiled.agent_catalog_snapshot_hash != expected["agent_catalog_snapshot_hash"]:
        raise SnapshotIntegrityError("compiled snapshot does not match agent catalog snapshot")
    if compiled.integration_base_commit != str(row["integration_base_commit"]):
        raise SnapshotIntegrityError("compiled snapshot base commit does not match run")
    if compiled.policy_version != str(row["policy_version"]):
        raise SnapshotIntegrityError("compiled snapshot policy version does not match run")
    return author, compiled, layout, catalog


def _agent_catalog_hash(catalog: AgentCatalog) -> str:
    return compute_agent_catalog_hash(catalog.agents)


def _run_record(row: aiosqlite.Row) -> WorkflowRunRecord:
    author, compiled, layout, catalog = _validated_snapshot_models(row)
    return WorkflowRunRecord(
        workflow_run_id=str(row["id"]),
        workflow_id=str(row["workflow_id"]),
        session_id=str(row["session_id"]),
        integration_base_commit=str(row["integration_base_commit"]),
        current_commit=str(row["current_commit"]),
        workflow_semantic_version=int(row["workflow_semantic_version"]),
        workflow_layout_version=int(row["workflow_layout_version"]),
        author_snapshot=author,
        author_snapshot_hash=str(row["author_snapshot_hash"]),
        compiled_snapshot=compiled,
        compiled_snapshot_hash=str(row["compiled_snapshot_hash"]),
        layout_snapshot=layout,
        layout_snapshot_hash=str(row["layout_snapshot_hash"]),
        policy_version=str(row["policy_version"]),
        agent_catalog_snapshot=catalog,
        agent_catalog_snapshot_hash=str(row["agent_catalog_snapshot_hash"]),
        planner_run_id=(str(row["planner_run_id"]) if row["planner_run_id"] is not None else None),
        planner_id=str(row["planner_id"]) if row["planner_id"] is not None else None,
        planner_model=(str(row["planner_model"]) if row["planner_model"] is not None else None),
        status=WorkflowRunStatus(str(row["status"])),
        created_at=str(row["created_at"]),
        started_at=str(row["started_at"]) if row["started_at"] is not None else None,
        finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
        cancel_requested_at=(
            str(row["cancel_requested_at"]) if row["cancel_requested_at"] is not None else None
        ),
        merge_finalizing_at=(
            str(row["merge_finalizing_at"]) if row["merge_finalizing_at"] is not None else None
        ),
    )


def _node_record(row: aiosqlite.Row) -> NodeRunRecord:
    return NodeRunRecord(
        node_run_id=str(row["id"]),
        workflow_run_id=str(row["workflow_run_id"]),
        node_id=str(row["node_id"]),
        node_type=NodeType(str(row["node_type"])),
        attempt=int(row["attempt"]),
        status=NodeRunStatus(str(row["status"])),
        outcome=NodeOutcome(str(row["outcome"])) if row["outcome"] is not None else None,
        assigned_agent_id=(
            str(row["assigned_agent_id"]) if row["assigned_agent_id"] is not None else None
        ),
        output_artifact_id=(
            str(row["output_artifact_id"]) if row["output_artifact_id"] is not None else None
        ),
        error_code=str(row["error_code"]) if row["error_code"] is not None else None,
        created_at=str(row["created_at"]),
        started_at=str(row["started_at"]) if row["started_at"] is not None else None,
        finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
    )


def _task_record(row: aiosqlite.Row) -> TaskRecord:
    return TaskRecord(
        task_id=str(row["id"]),
        node_run_id=str(row["node_run_id"]),
        agent_id=str(row["agent_id"]),
        base_commit=str(row["base_commit"]),
        runtime_policy_artifact_id=(
            str(row["runtime_policy_artifact_id"])
            if row["runtime_policy_artifact_id"] is not None
            else None
        ),
        active_capability_grant_id=(
            str(row["active_capability_grant_id"])
            if row["active_capability_grant_id"] is not None
            else None
        ),
        status=TaskStatus(str(row["status"])),
        created_at=str(row["created_at"]),
        finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
    )


__all__ = [
    "NewWorkflowRun",
    "NodeRunRecord",
    "TaskRecord",
    "WorkflowRunRecord",
    "WorkflowRunRepository",
    "node_run_id",
    "task_id_for_node",
]
