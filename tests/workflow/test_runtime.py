from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from adapters.mock import MockAgentAdapter
from context.task_bundle import TaskContextBundle
from protocol import (
    ArtifactType,
    AssignmentMode,
    AuthorGraph,
    EdgeCondition,
    IfCondition,
    IfOperator,
    NodeOutcome,
    NodeRunStatus,
    NodeType,
    RiskLevel,
    TaskKind,
    TaskStatus,
    WorkflowEdge,
    WorkflowLayout,
    WorkflowNode,
    WorkflowRunStatus,
)
from storage.agent_repository import AgentRepository
from storage.artifact_repository import ArtifactRepository
from storage.artifact_store import ArtifactStore
from storage.db import Database
from storage.errors import LeaseLost, SnapshotIntegrityError
from storage.event_repository import EventRepository
from storage.leases import MasterLease, MasterLeaseRepository, WorkspaceLeaseRepository
from storage.repositories import (
    NewSession,
    NewWorkflow,
    SessionRepository,
    WorkflowRepository,
)
from storage.workflow_run_repository import NewWorkflowRun, WorkflowRunRepository
from workflow.compiler import WorkflowCompiler
from workflow.events import build_runtime_event_registry
from workflow.executable_validator import ExecutableValidator
from workflow.executor import GraphExecutor
from workflow.handlers.agent_task import AgentTaskNodeHandler
from workflow.handlers.factory import build_node_registry
from workflow.scheduler import DurableScheduler


@pytest.mark.asyncio
async def test_readonly_mock_workflow_is_durable_and_replayable(
    runtime_database: Database,
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(runtime_database, tmp_path)
    session, workflow = await _seed_workflow(
        runtime_database,
        fixture_source_repo,
        _readonly_graph(),
    )
    catalog = await runtime.agents.catalog()
    compilation = WorkflowCompiler(catalog).compile(
        workflow.author_graph,
        integration_base_commit=session.integration_head_commit,
    )
    assert compilation.ok
    assert compilation.graph is not None
    assert ExecutableValidator(runtime.registry).validate(compilation.graph).ok

    lease = await runtime.leases.acquire(
        instance_id="test-master",
        process_id=1234,
        ttl_seconds=60,
    )
    created = await _create_run(
        runtime,
        NewWorkflowRun(
            workflow_run_id="run-readonly",
            workflow=workflow,
            compiled_graph=compilation.graph,
            agent_catalog=catalog,
            current_commit=session.integration_head_commit,
        ),
        lease=lease,
    )
    assert created.status == WorkflowRunStatus.PENDING

    completed = await runtime.scheduler.run_until_stable(created.workflow_run_id, lease=lease)

    assert completed.status == WorkflowRunStatus.COMPLETED
    nodes = await runtime.runs.list_nodes(created.workflow_run_id)
    assert {node.status for node in nodes} == {NodeRunStatus.COMPLETED}
    assert all(node.output_artifact_id for node in nodes)
    task = await _only_task(runtime_database)
    assert task["status"] == TaskStatus.SUCCEEDED.value
    events = await runtime.events.list_by_run(created.workflow_run_id, limit=100)
    assert [event.run_seq for event in events] == list(range(1, len(events) + 1))
    assert any(event.event_type == "workflow.node_state_changed" for event in events)

    edited = workflow.author_graph.model_copy(deep=True)
    edited.nodes[1].title = "Edited after run"
    await WorkflowRepository(runtime_database).update_author_graph(
        workflow.workflow_id,
        edited,
        expected_semantic_version=workflow.semantic_version,
    )
    replay = await runtime.runs.get(created.workflow_run_id)
    snapshot_task = next(
        node for node in replay.compiled_snapshot.nodes if node.node_type == NodeType.AGENT_TASK
    )
    assert snapshot_task.title == "Analyze fixture"


@pytest.mark.asyncio
async def test_if_branch_marks_inactive_path_skipped(
    runtime_database: Database,
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(runtime_database, tmp_path)
    session, workflow = await _seed_workflow(
        runtime_database,
        fixture_source_repo,
        _branch_graph(),
        session_id="session-branch",
        workflow_id="workflow-branch",
    )
    catalog = await runtime.agents.catalog()
    compilation = WorkflowCompiler(catalog).compile(
        workflow.author_graph,
        integration_base_commit=session.integration_head_commit,
    )
    assert compilation.ok and compilation.graph is not None
    assert ExecutableValidator(runtime.registry).validate(compilation.graph).ok
    lease = await runtime.leases.acquire(
        instance_id="branch-master",
        process_id=1235,
        ttl_seconds=60,
    )
    await _create_run(
        runtime,
        NewWorkflowRun(
            workflow_run_id="run-branch",
            workflow=workflow,
            compiled_graph=compilation.graph,
            agent_catalog=catalog,
            current_commit=session.integration_head_commit,
        ),
        lease=lease,
    )

    result = await runtime.scheduler.run_until_stable("run-branch", lease=lease)

    assert result.status == WorkflowRunStatus.COMPLETED
    by_node = {node.node_id: node for node in await runtime.runs.list_nodes("run-branch")}
    assert by_node["branch"].outcome == NodeOutcome.MATCHED
    assert by_node["matched-task"].status == NodeRunStatus.COMPLETED
    assert by_node["other-task"].status == NodeRunStatus.SKIPPED


@pytest.mark.asyncio
async def test_if_reads_a_dominating_transitive_predecessor(
    runtime_database: Database,
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(runtime_database, tmp_path)
    session, workflow = await _seed_workflow(
        runtime_database,
        fixture_source_repo,
        _transitive_branch_graph(),
        session_id="session-transitive-if",
        workflow_id="workflow-transitive-if",
    )
    catalog = await runtime.agents.catalog()
    compilation = WorkflowCompiler(catalog).compile(
        workflow.author_graph,
        integration_base_commit=session.integration_head_commit,
    )
    assert compilation.ok and compilation.graph is not None
    lease = await runtime.leases.acquire(
        instance_id="transitive-if-master",
        process_id=1239,
        ttl_seconds=60,
    )
    await _create_run(
        runtime,
        NewWorkflowRun(
            workflow_run_id="run-transitive-if",
            workflow=workflow,
            compiled_graph=compilation.graph,
            agent_catalog=catalog,
            current_commit=session.integration_head_commit,
        ),
        lease=lease,
    )

    result = await runtime.scheduler.run_until_stable("run-transitive-if", lease=lease)

    assert result.status == WorkflowRunStatus.COMPLETED
    by_node = {node.node_id: node for node in await runtime.runs.list_nodes("run-transitive-if")}
    assert by_node["branch"].outcome == NodeOutcome.MATCHED
    assert by_node["output"].outcome == NodeOutcome.SUCCESS


@pytest.mark.asyncio
async def test_stale_master_cannot_advance_pending_run(
    runtime_database: Database,
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(runtime_database, tmp_path)
    session, workflow = await _seed_workflow(
        runtime_database,
        fixture_source_repo,
        _readonly_graph(),
        session_id="session-fence",
        workflow_id="workflow-fence",
    )
    catalog = await runtime.agents.catalog()
    compilation = WorkflowCompiler(catalog).compile(
        workflow.author_graph,
        integration_base_commit=session.integration_head_commit,
    )
    assert compilation.ok and compilation.graph is not None
    started = datetime.now(UTC)
    stale = await runtime.leases.acquire(
        instance_id="stale-master",
        process_id=2001,
        ttl_seconds=30,
        now=started,
    )
    await _create_run(
        runtime,
        NewWorkflowRun(
            workflow_run_id="run-fence",
            workflow=workflow,
            compiled_graph=compilation.graph,
            agent_catalog=catalog,
            current_commit=session.integration_head_commit,
        ),
        lease=stale,
        now=started,
    )
    replacement_time = started + timedelta(seconds=31)
    replacement = await runtime.leases.acquire(
        instance_id="replacement-master",
        process_id=2002,
        ttl_seconds=60,
        now=replacement_time,
    )

    with pytest.raises(LeaseLost):
        await runtime.runs.start_and_reconcile(
            "run-fence",
            lease=stale,
            now=replacement_time,
        )

    unchanged = await runtime.runs.get("run-fence")
    assert unchanged.status == WorkflowRunStatus.PENDING
    assert len(await runtime.events.list_by_run("run-fence")) == 1
    completed = await runtime.scheduler.run_until_stable("run-fence", lease=replacement)
    assert completed.status == WorkflowRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_stale_workspace_owner_cannot_create_run_snapshot(
    runtime_database: Database,
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(runtime_database, tmp_path)
    session, workflow = await _seed_workflow(
        runtime_database,
        fixture_source_repo,
        _readonly_graph(),
        session_id="session-workspace-fence",
        workflow_id="workflow-workspace-fence",
    )
    catalog = await runtime.agents.catalog()
    compilation = WorkflowCompiler(catalog).compile(
        workflow.author_graph,
        integration_base_commit=session.integration_head_commit,
    )
    assert compilation.ok and compilation.graph is not None
    started = datetime.now(UTC)
    master = await runtime.leases.acquire(
        instance_id="workspace-fence-master",
        process_id=2010,
        ttl_seconds=120,
        now=started,
    )
    stale_workspace = await runtime.workspace_leases.acquire(
        resource_key=f"session:{session.session_id}:integration",
        owner_kind="run",
        owner_operation_id="stale-run-snapshot",
        owner_process_id=2010,
        ttl_seconds=30,
        now=started,
    )
    replacement_time = started + timedelta(seconds=31)
    await runtime.workspace_leases.acquire(
        resource_key=f"session:{session.session_id}:integration",
        owner_kind="run",
        owner_operation_id="replacement-run-snapshot",
        owner_process_id=2011,
        ttl_seconds=60,
        now=replacement_time,
    )

    with pytest.raises(LeaseLost):
        await runtime.runs.create(
            NewWorkflowRun(
                workflow_run_id="run-workspace-fence",
                workflow=workflow,
                compiled_graph=compilation.graph,
                agent_catalog=catalog,
                current_commit=session.integration_head_commit,
            ),
            lease=master,
            workspace_lease=stale_workspace,
            now=replacement_time,
        )

    async with runtime_database.connection() as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*) AS count FROM workflow_runs WHERE id = ?",
            ("run-workspace-fence",),
        )
        row = await cursor.fetchone()
        await cursor.close()
    assert row is not None and int(row["count"]) == 0


@pytest.mark.asyncio
async def test_snapshot_hash_mismatch_stops_scheduler_before_mutation(
    runtime_database: Database,
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(runtime_database, tmp_path)
    session, workflow = await _seed_workflow(
        runtime_database,
        fixture_source_repo,
        _readonly_graph(),
        session_id="session-tamper",
        workflow_id="workflow-tamper",
    )
    catalog = await runtime.agents.catalog()
    compilation = WorkflowCompiler(catalog).compile(
        workflow.author_graph,
        integration_base_commit=session.integration_head_commit,
    )
    assert compilation.graph is not None
    lease = await runtime.leases.acquire(
        instance_id="tamper-master",
        process_id=1236,
        ttl_seconds=60,
    )
    await _create_run(
        runtime,
        NewWorkflowRun(
            workflow_run_id="run-tamper",
            workflow=workflow,
            compiled_graph=compilation.graph,
            agent_catalog=catalog,
            current_commit=session.integration_head_commit,
        ),
        lease=lease,
    )
    async with runtime_database.immediate_transaction() as transaction:
        row = await transaction.fetch_one(
            "SELECT compiled_snapshot_json FROM workflow_runs WHERE id = ?",
            ("run-tamper",),
        )
        assert row is not None
        payload = json.loads(str(row["compiled_snapshot_json"]))
        payload["nodes"][0]["title"] = "Tampered after snapshot"
        await transaction.execute(
            "UPDATE workflow_runs SET compiled_snapshot_json = ? WHERE id = ?",
            (json.dumps(payload), "run-tamper"),
        )

    with pytest.raises(SnapshotIntegrityError):
        await runtime.runs.get("run-tamper")
    with pytest.raises(SnapshotIntegrityError):
        await runtime.scheduler.tick("run-tamper", lease=lease)

    async with runtime_database.connection() as connection:
        cursor = await connection.execute(
            "SELECT status FROM workflow_runs WHERE id = ?",
            ("run-tamper",),
        )
        row = await cursor.fetchone()
        await cursor.close()
    assert row is not None and row["status"] == WorkflowRunStatus.PENDING.value
    assert len(await runtime.events.list_by_run("run-tamper")) == 1


@pytest.mark.asyncio
async def test_serial_agents_copy_upstream_output_into_current_task_scope(
    runtime_database: Database,
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(runtime_database, tmp_path)
    session, workflow = await _seed_workflow(
        runtime_database,
        fixture_source_repo,
        _serial_agent_graph(),
        session_id="session-serial",
        workflow_id="workflow-serial",
    )
    catalog = await runtime.agents.catalog()
    compilation = WorkflowCompiler(catalog).compile(
        workflow.author_graph,
        integration_base_commit=session.integration_head_commit,
    )
    assert compilation.graph is not None
    lease = await runtime.leases.acquire(
        instance_id="serial-master",
        process_id=1237,
        ttl_seconds=60,
    )
    await _create_run(
        runtime,
        NewWorkflowRun(
            workflow_run_id="run-serial",
            workflow=workflow,
            compiled_graph=compilation.graph,
            agent_catalog=catalog,
            current_commit=session.integration_head_commit,
        ),
        lease=lease,
    )

    completed = await runtime.scheduler.run_until_stable("run-serial", lease=lease)

    assert completed.status == WorkflowRunStatus.COMPLETED
    async with runtime_database.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT a.task_id, a.redacted, nr.node_id
            FROM artifacts a
            JOIN tasks t ON a.task_id = t.id
            JOIN node_runs nr ON t.node_run_id = nr.id
            WHERE a.id LIKE 'context-copy-%'
            """
        )
        copies = await cursor.fetchall()
        await cursor.close()
        cursor = await connection.execute(
            """
            SELECT t.id AS task_id, a.task_id AS policy_owner, a.artifact_type
            FROM tasks t
            JOIN artifacts a ON t.runtime_policy_artifact_id = a.id
            ORDER BY t.id
            """
        )
        policies = await cursor.fetchall()
        await cursor.close()
    assert len(copies) == 1
    assert copies[0]["node_id"] == "review"
    assert copies[0]["redacted"] == 1
    assert len(policies) == 2
    assert all(row["task_id"] == row["policy_owner"] for row in policies)
    assert all(row["artifact_type"] == "runtime_policy" for row in policies)


@pytest.mark.asyncio
async def test_task_start_and_both_events_roll_back_as_one_transaction(
    runtime_database: Database,
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(runtime_database, tmp_path)
    session, workflow = await _seed_workflow(
        runtime_database,
        fixture_source_repo,
        _readonly_graph(),
        session_id="session-task-atomic",
        workflow_id="workflow-task-atomic",
    )
    catalog = await runtime.agents.catalog()
    compilation = WorkflowCompiler(catalog).compile(
        workflow.author_graph,
        integration_base_commit=session.integration_head_commit,
    )
    assert compilation.graph is not None
    lease = await runtime.leases.acquire(
        instance_id="task-atomic-master",
        process_id=1238,
        ttl_seconds=60,
    )
    await _create_run(
        runtime,
        NewWorkflowRun(
            workflow_run_id="run-task-atomic",
            workflow=workflow,
            compiled_graph=compilation.graph,
            agent_catalog=catalog,
            current_commit=session.integration_head_commit,
        ),
        lease=lease,
    )
    await runtime.scheduler.tick("run-task-atomic", lease=lease)
    claimed = await runtime.runs.claim_next("run-task-atomic", lease=lease)
    assert claimed is not None and claimed.node_id == "analyze"
    policy = await runtime.artifacts.create(
        artifact_id="policy-task-atomic",
        session_id=session.session_id,
        artifact_type=ArtifactType.RUNTIME_POLICY,
        content=b"{}",
        redacted=True,
    )
    before_events = await runtime.events.list_by_run("run-task-atomic")
    async with runtime_database.connection() as connection:
        cursor = await connection.executescript(
            """
            CREATE TRIGGER fail_running_task_event
            BEFORE INSERT ON events
            WHEN NEW.event_type = 'workflow.task_state_changed'
              AND json_extract(NEW.payload_json, '$.status') = 'running'
            BEGIN
                SELECT RAISE(ABORT, 'injected task event failure');
            END;
            """
        )
        await cursor.close()

    with pytest.raises(aiosqlite.IntegrityError):
        await runtime.runs.create_and_start_task(
            task_id="task-atomic",
            node_run_id_value=claimed.node_run_id,
            agent_id="mock",
            base_commit=session.integration_head_commit,
            runtime_policy_artifact_id=policy.artifact_id,
            lease=lease,
        )

    async with runtime_database.connection() as connection:
        cursor = await connection.execute("SELECT COUNT(*) AS count FROM tasks")
        task_count = int((await cursor.fetchone())["count"])
        await cursor.close()
        cursor = await connection.execute(
            "SELECT task_id FROM artifacts WHERE id = ?",
            (policy.artifact_id,),
        )
        policy_row = await cursor.fetchone()
        await cursor.close()
    after_events = await runtime.events.list_by_run("run-task-atomic")
    assert task_count == 0
    assert policy_row is not None and policy_row["task_id"] is None
    assert len(after_events) == len(before_events)


class _Runtime:
    def __init__(
        self,
        *,
        agents: AgentRepository,
        leases: MasterLeaseRepository,
        workspace_leases: WorkspaceLeaseRepository,
        events: EventRepository,
        runs: WorkflowRunRepository,
        artifacts: ArtifactRepository,
        registry,
        scheduler: DurableScheduler,
    ) -> None:
        self.agents = agents
        self.leases = leases
        self.workspace_leases = workspace_leases
        self.events = events
        self.runs = runs
        self.artifacts = artifacts
        self.registry = registry
        self.scheduler = scheduler


async def _build_runtime(database: Database, tmp_path: Path) -> _Runtime:
    agents = AgentRepository(database)
    await agents.register_mock()
    leases = MasterLeaseRepository(database)
    workspace_leases = WorkspaceLeaseRepository(database)
    events = EventRepository(database, build_runtime_event_registry())
    runs = WorkflowRunRepository(database, events, leases, workspace_leases)
    artifact_repository = ArtifactRepository(
        database,
        ArtifactStore(tmp_path / "artifacts"),
        max_artifact_bytes=1_000_000,
        max_session_artifact_bytes=10_000_000,
    )
    bundle = TaskContextBundle(
        artifact_repository,
        tmp_path / "agent-runs",
        max_bundle_bytes=1_000_000,
        ttl_seconds=300,
    )
    agent_handler = AgentTaskNodeHandler(
        runs,
        artifact_repository,
        bundle,
        {"mock": MockAgentAdapter()},
    )
    registry = build_node_registry(agent_handler)
    executor = GraphExecutor(
        runs,
        SessionRepository(database),
        artifact_repository,
        registry,
    )
    return _Runtime(
        agents=agents,
        leases=leases,
        workspace_leases=workspace_leases,
        events=events,
        runs=runs,
        artifacts=artifact_repository,
        registry=registry,
        scheduler=DurableScheduler(runs, executor, poll_interval_seconds=0.01),
    )


async def _create_run(
    runtime: _Runtime,
    value: NewWorkflowRun,
    *,
    lease: MasterLease,
    now: datetime | None = None,
):
    workspace_lease = await runtime.workspace_leases.acquire(
        resource_key=f"session:{value.workflow.session_id}:integration",
        owner_kind="test-run",
        owner_operation_id=f"snapshot-{value.workflow_run_id}",
        owner_process_id=1234,
        ttl_seconds=60,
        now=now,
    )
    try:
        return await runtime.runs.create(
            value,
            lease=lease,
            workspace_lease=workspace_lease,
            now=now,
        )
    finally:
        await runtime.workspace_leases.release(workspace_lease, now=now)


async def _seed_workflow(
    database: Database,
    repo: Path,
    graph: AuthorGraph,
    *,
    session_id: str = "session-readonly",
    workflow_id: str = "workflow-readonly",
):
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    session = await SessionRepository(database).create(
        NewSession(
            session_id=session_id,
            goal="Analyze the fixture repository without changes",
            source_repo_path=repo,
            shared_repo_path=repo,
            base_commit=commit,
            integration_branch="main",
            integration_head_commit=commit,
        )
    )
    workflow = await WorkflowRepository(database).create(
        NewWorkflow(
            workflow_id=workflow_id,
            session_id=session.session_id,
            author_graph=graph,
            layout=WorkflowLayout(),
        )
    )
    return session, workflow


async def _only_task(database: Database):
    async with database.connection() as connection:
        cursor = await connection.execute("SELECT * FROM tasks")
        rows = await cursor.fetchall()
        await cursor.close()
    assert len(rows) == 1
    return rows[0]


def _readonly_graph() -> AuthorGraph:
    return AuthorGraph(
        nodes=[
            WorkflowNode(id="input", node_type=NodeType.INPUT, title="Input"),
            WorkflowNode(
                id="analyze",
                node_type=NodeType.AGENT_TASK,
                task_kind=TaskKind.ANALYZE,
                title="Analyze fixture",
                instruction="Inspect the provided metadata and return a bounded summary.",
                assigned_agent="mock",
                assignment_mode=AssignmentMode.LOCKED,
                allowed_files_candidate=["src/example.py"],
                risk_level_hint=RiskLevel.L0,
            ),
            WorkflowNode(id="output", node_type=NodeType.OUTPUT, title="Output"),
        ],
        edges=[
            WorkflowEdge(id="e1", from_node="input", to_node="analyze"),
            WorkflowEdge(id="e2", from_node="analyze", to_node="output"),
        ],
    )


def _serial_agent_graph() -> AuthorGraph:
    return AuthorGraph(
        nodes=[
            WorkflowNode(id="input", node_type=NodeType.INPUT, title="Input"),
            WorkflowNode(
                id="analyze",
                node_type=NodeType.AGENT_TASK,
                task_kind=TaskKind.ANALYZE,
                title="Analyze",
                instruction="Produce a bounded read-only analysis.",
                assigned_agent="mock",
                assignment_mode=AssignmentMode.LOCKED,
                risk_level_hint=RiskLevel.L0,
            ),
            WorkflowNode(
                id="review",
                node_type=NodeType.AGENT_TASK,
                task_kind=TaskKind.REVIEW,
                title="Review",
                instruction="Review the upstream analysis artifact.",
                assigned_agent="mock",
                assignment_mode=AssignmentMode.LOCKED,
                risk_level_hint=RiskLevel.L0,
            ),
            WorkflowNode(id="output", node_type=NodeType.OUTPUT, title="Output"),
        ],
        edges=[
            WorkflowEdge(id="e1", from_node="input", to_node="analyze"),
            WorkflowEdge(id="e2", from_node="analyze", to_node="review"),
            WorkflowEdge(id="e3", from_node="review", to_node="output"),
        ],
    )


def _transitive_branch_graph() -> AuthorGraph:
    return AuthorGraph(
        nodes=[
            WorkflowNode(id="input", node_type=NodeType.INPUT, title="Input"),
            WorkflowNode(
                id="analyze",
                node_type=NodeType.AGENT_TASK,
                task_kind=TaskKind.ANALYZE,
                title="Analyze",
                instruction="Analyze read-only metadata.",
                assigned_agent="mock",
                assignment_mode=AssignmentMode.LOCKED,
                risk_level_hint=RiskLevel.L0,
            ),
            WorkflowNode(
                id="context",
                node_type=NodeType.CONTEXT_BUILDER,
                title="Build context",
            ),
            WorkflowNode(
                id="branch",
                node_type=NodeType.IF,
                title="Check analysis",
                if_condition=IfCondition(
                    upstream_node_id="analyze",
                    field="outcome",
                    operator=IfOperator.EQ,
                    value="success",
                ),
            ),
            WorkflowNode(id="output", node_type=NodeType.OUTPUT, title="Output"),
        ],
        edges=[
            WorkflowEdge(id="e1", from_node="input", to_node="analyze"),
            WorkflowEdge(id="e2", from_node="analyze", to_node="context"),
            WorkflowEdge(id="e3", from_node="context", to_node="branch"),
            WorkflowEdge(
                id="e4",
                from_node="branch",
                to_node="output",
                condition=EdgeCondition.MATCHED,
            ),
            WorkflowEdge(
                id="e5",
                from_node="branch",
                to_node="output",
                condition=EdgeCondition.NOT_MATCHED,
            ),
        ],
    )


def _branch_graph() -> AuthorGraph:
    return AuthorGraph(
        nodes=[
            WorkflowNode(id="input", node_type=NodeType.INPUT, title="Input"),
            WorkflowNode(
                id="analyze",
                node_type=NodeType.AGENT_TASK,
                task_kind=TaskKind.ANALYZE,
                title="Analyze",
                instruction="Analyze read-only metadata.",
                assigned_agent="mock",
                assignment_mode=AssignmentMode.LOCKED,
                risk_level_hint=RiskLevel.L0,
            ),
            WorkflowNode(
                id="branch",
                node_type=NodeType.IF,
                title="Check result",
                if_condition=IfCondition(
                    upstream_node_id="analyze",
                    field="outcome",
                    operator=IfOperator.EQ,
                    value="success",
                ),
            ),
            WorkflowNode(
                id="matched-task",
                node_type=NodeType.AGENT_TASK,
                task_kind=TaskKind.REVIEW,
                title="Review success",
                instruction="Review the successful analysis.",
                assigned_agent="mock",
                assignment_mode=AssignmentMode.LOCKED,
                risk_level_hint=RiskLevel.L0,
            ),
            WorkflowNode(
                id="other-task",
                node_type=NodeType.AGENT_TASK,
                task_kind=TaskKind.REVIEW,
                title="Review mismatch",
                instruction="Review the mismatched analysis.",
                assigned_agent="mock",
                assignment_mode=AssignmentMode.LOCKED,
                risk_level_hint=RiskLevel.L0,
            ),
            WorkflowNode(id="output", node_type=NodeType.OUTPUT, title="Output"),
        ],
        edges=[
            WorkflowEdge(id="e1", from_node="input", to_node="analyze"),
            WorkflowEdge(id="e2", from_node="analyze", to_node="branch"),
            WorkflowEdge(
                id="e3",
                from_node="branch",
                to_node="matched-task",
                condition=EdgeCondition.MATCHED,
            ),
            WorkflowEdge(
                id="e4",
                from_node="branch",
                to_node="other-task",
                condition=EdgeCondition.NOT_MATCHED,
            ),
            WorkflowEdge(id="e5", from_node="matched-task", to_node="output"),
            WorkflowEdge(id="e6", from_node="other-task", to_node="output"),
        ],
    )
