from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.services import WorkflowApplication
from context.task_bundle import CleanupResult
from protocol import (
    ArtifactType,
    AssignmentMode,
    AuthorGraph,
    ChangeSetStatus,
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
from storage.errors import (
    ConcurrencyConflict,
    ContainmentViolation,
    LeaseLost,
    SnapshotIntegrityError,
)
from storage.repositories import NewWorkflow
from storage.workflow_run_repository import node_run_id
from workspace.lock_manager import WorkspaceOwnerKind


@pytest.mark.asyncio
async def test_mock_write_reaches_hub210_gate_and_restores_shared_repo(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    application = WorkflowApplication(Settings(data_dir=tmp_path / "agent-hub-data"))
    await application.initialize()
    await application.register_mock_agent()
    session = await application.create_session(
        repo=fixture_source_repo,
        goal="Create a bounded documentation demo file.",
        session_id="session-hub200",
    )
    workflow = await application.services.workflows.create(
        NewWorkflow(
            workflow_id="workflow-hub200",
            session_id=session.session_id,
            author_graph=_docs_write_graph(),
            layout=WorkflowLayout(),
        )
    )

    async with application.temporary_master() as lease:
        run = await application.run(
            workflow.workflow_id,
            lease=lease,
            workflow_run_id="run-hub200",
        )

    assert run.status == WorkflowRunStatus.BLOCKED
    assert application.services.git.state(session.shared_repo_path).dirty is False
    assert not (session.shared_repo_path / "docs" / "agent-hub-demo.md").exists()

    node_runs = await application.services.runs.list_nodes(run.workflow_run_id)
    by_type = {node.node_type: node for node in node_runs}
    assert by_type[NodeType.PATCH_GUARD].status == NodeRunStatus.COMPLETED
    assert by_type[NodeType.TEST].status == NodeRunStatus.COMPLETED
    assert by_type[NodeType.RISK_CLASSIFIER].status == NodeRunStatus.COMPLETED
    assert by_type[NodeType.APPROVAL].status == NodeRunStatus.BLOCKED_BY_GUARD

    async with application.services.database.connection() as connection:
        cursor = await connection.execute("SELECT id FROM change_sets")
        rows = await cursor.fetchall()
        await cursor.close()
    assert len(rows) == 1
    record = await application.services.change_sets.get(str(rows[0]["id"]))
    assert record.change_set.status == ChangeSetStatus.TEST_PASSED
    assert record.change_set.created_files == ["docs/agent-hub-demo.md"]
    assert record.change_set.patch_sha256 == record.change_set.canonical_patch_ref.sha256

    events = await application.show_events(workflow_run_id=run.workflow_run_id)
    change_events = [
        event for event in events if event.event_type == "workflow.change_set_state_changed"
    ]
    assert len(change_events) == 3

    async with application.temporary_master() as lease:
        with pytest.raises(ConcurrencyConflict):
            await application.services.change_sets.transition(
                record.change_set.change_set_id,
                expected=ChangeSetStatus.CAPTURED,
                target=ChangeSetStatus.GUARD_REJECTED,
                master_lease=lease,
            )

    patch_ref = record.change_set.canonical_patch_ref
    patch_path = application.services.artifacts.store.resolve(
        patch_ref.artifact_id,
        patch_ref.artifact_type.value,
    )
    patch_path.write_bytes(b"tampered patch")
    with pytest.raises(ContainmentViolation):
        await application.services.change_sets.load_patch(
            record.change_set.change_set_id,
        )


@pytest.mark.parametrize("failure_point", ["cleanup", "output_artifact", "finish_task"])
@pytest.mark.asyncio
async def test_post_capture_failure_abandons_change_set_and_fails_task(
    fixture_source_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    application = WorkflowApplication(Settings(data_dir=tmp_path / "agent-hub-data"))
    await application.initialize()
    await application.register_mock_agent()
    session = await application.create_session(
        repo=fixture_source_repo,
        goal="Inject a post-capture failure.",
        session_id=f"session-post-capture-{failure_point}",
    )
    workflow = await application.services.workflows.create(
        NewWorkflow(
            workflow_id=f"workflow-post-capture-{failure_point}",
            session_id=session.session_id,
            author_graph=_docs_write_graph(),
            layout=WorkflowLayout(),
        )
    )

    injected = False
    if failure_point == "cleanup":
        agent_handler = application.services.registry.get_handler(_docs_write_graph().nodes[1])
        bundles = agent_handler._bundles
        original_cleanup = bundles.cleanup

        async def fail_cleanup_once(task_id: str) -> CleanupResult:
            nonlocal injected
            if not injected:
                injected = True
                return CleanupResult(removed_files=0, removed_dirs=0, errors=["injected"])
            return await original_cleanup(task_id)

        monkeypatch.setattr(bundles, "cleanup", fail_cleanup_once)
    elif failure_point == "output_artifact":
        original_create = application.services.artifacts.create

        async def fail_output_once(**kwargs: object):
            nonlocal injected
            if (
                not injected
                and str(kwargs["artifact_id"]).startswith("mock-output-")
                and kwargs["artifact_type"] == ArtifactType.REPORT
            ):
                injected = True
                raise RuntimeError("injected output artifact failure")
            return await original_create(**kwargs)

        monkeypatch.setattr(application.services.artifacts, "create", fail_output_once)
    else:
        original_finish = application.services.runs.finish_task

        async def fail_success_finish_once(task_id: str, **kwargs: object):
            nonlocal injected
            if not injected and kwargs["target"] == TaskStatus.SUCCEEDED:
                injected = True
                raise RuntimeError("injected task finalization failure")
            return await original_finish(task_id, **kwargs)

        monkeypatch.setattr(application.services.runs, "finish_task", fail_success_finish_once)

    async with application.temporary_master() as lease:
        run = await application.run(
            workflow.workflow_id,
            lease=lease,
            workflow_run_id=f"run-post-capture-{failure_point}",
        )

    assert injected is True
    assert run.status == WorkflowRunStatus.FAILED
    record = await application.services.change_sets.get_for_source_node(
        workflow_run_id=run.workflow_run_id,
        source_node_id="write-docs",
    )
    assert record.change_set.status == ChangeSetStatus.ABANDONED_PARTIAL
    task = await application.services.runs.get_task(record.change_set.task_id)
    assert task.status == TaskStatus.FAILED
    node_runs = await application.services.runs.list_nodes(run.workflow_run_id)
    write_node = next(node for node in node_runs if node.node_id == "write-docs")
    assert write_node.status == NodeRunStatus.FAILED
    assert application.services.git.state(session.shared_repo_path).dirty is False
    assert not (session.shared_repo_path / "docs" / "agent-hub-demo.md").exists()

    async with application.services.database.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT severity, payload_json FROM security_events
            WHERE workflow_run_id = ? AND task_id = ?
            """,
            (run.workflow_run_id, record.change_set.task_id),
        )
        security_events = await cursor.fetchall()
        await cursor.close()
    assert len(security_events) == 1
    assert security_events[0]["severity"] == "high"
    assert '"status":"abandoned_partial"' in security_events[0]["payload_json"]


@pytest.mark.asyncio
async def test_workspace_lease_loss_does_not_finalize_task_or_node(
    fixture_source_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = WorkflowApplication(Settings(data_dir=tmp_path / "agent-hub-data"))
    await application.initialize()
    await application.register_mock_agent()
    session = await application.create_session(
        repo=fixture_source_repo,
        goal="Stop a stale writer before capture.",
        session_id="session-write-lease-lost",
    )
    workflow = await application.services.workflows.create(
        NewWorkflow(
            workflow_id="workflow-write-lease-lost",
            session_id=session.session_id,
            author_graph=_docs_write_graph(),
            layout=WorkflowLayout(),
        )
    )

    async def lose_before_capture(*args: object, **kwargs: object) -> None:
        _ = args, kwargs
        raise LeaseLost("injected workspace lease loss")

    monkeypatch.setattr(application.services.locks, "run_fenced", lose_before_capture)

    async with application.temporary_master() as lease:
        with pytest.raises(LeaseLost, match="injected workspace lease loss"):
            await application.run(
                workflow.workflow_id,
                lease=lease,
                workflow_run_id="run-write-lease-lost",
            )

    run = await application.services.runs.get("run-write-lease-lost")
    assert run.status == WorkflowRunStatus.RUNNING
    node_runs = await application.services.runs.list_nodes(run.workflow_run_id)
    write_node = next(node for node in node_runs if node.node_id == "write-docs")
    assert write_node.status == NodeRunStatus.RUNNING
    async with application.services.database.connection() as connection:
        task_cursor = await connection.execute(
            "SELECT status FROM tasks WHERE node_run_id = ?",
            (write_node.node_run_id,),
        )
        task_row = await task_cursor.fetchone()
        await task_cursor.close()
        change_cursor = await connection.execute("SELECT COUNT(*) FROM change_sets")
        change_count = await change_cursor.fetchone()
        await change_cursor.close()
    assert task_row is not None and task_row["status"] == TaskStatus.RUNNING.value
    assert change_count is not None and change_count[0] == 0
    assert application.services.git.state(session.shared_repo_path).dirty is True
    assert (session.shared_repo_path / "docs" / "agent-hub-demo.md").exists()


@pytest.mark.asyncio
async def test_workspace_fence_advance_blocks_write_node_completion(
    fixture_source_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = WorkflowApplication(Settings(data_dir=tmp_path / "agent-hub-data"))
    await application.initialize()
    await application.register_mock_agent()
    session = await application.create_session(
        repo=fixture_source_repo,
        goal="Reject a stale node completion proof.",
        session_id="session-write-fence-advance",
    )
    workflow = await application.services.workflows.create(
        NewWorkflow(
            workflow_id="workflow-write-fence-advance",
            session_id=session.session_id,
            author_graph=_docs_write_graph(),
            layout=WorkflowLayout(),
        )
    )
    expected_node_run_id = node_run_id(
        "run-write-fence-advance",
        "write-docs",
        1,
    )
    original_complete = application.services.runs.complete_node
    replacement = None

    async def advance_fence_before_completion(
        node_run_id_value: str,
        **kwargs: object,
    ):
        nonlocal replacement
        if node_run_id_value == expected_node_run_id and replacement is None:
            replacement = await application.services.locks.acquire(
                session_id=session.session_id,
                owner_kind=WorkspaceOwnerKind.RECOVERY,
                owner_operation_id="recovery-before-node-completion",
                owner_process_id=9090,
                ttl_seconds=30,
            )
        return await original_complete(node_run_id_value, **kwargs)

    monkeypatch.setattr(
        application.services.runs,
        "complete_node",
        advance_fence_before_completion,
    )
    try:
        async with application.temporary_master() as lease:
            with pytest.raises(
                SnapshotIntegrityError,
                match="workspace fencing advanced",
            ):
                await application.run(
                    workflow.workflow_id,
                    lease=lease,
                    workflow_run_id="run-write-fence-advance",
                )
    finally:
        if replacement is not None:
            await application.services.locks.release(replacement)

    run = await application.services.runs.get("run-write-fence-advance")
    assert run.status == WorkflowRunStatus.RUNNING
    node_runs = await application.services.runs.list_nodes(run.workflow_run_id)
    write_node = next(node for node in node_runs if node.node_id == "write-docs")
    assert write_node.status == NodeRunStatus.RUNNING
    record = await application.services.change_sets.get_for_source_node(
        workflow_run_id=run.workflow_run_id,
        source_node_id="write-docs",
    )
    assert record.change_set.status == ChangeSetStatus.CAPTURED
    task = await application.services.runs.get_task(record.change_set.task_id)
    assert task.status == TaskStatus.SUCCEEDED
    assert application.services.git.state(session.shared_repo_path).dirty is False


def _docs_write_graph() -> AuthorGraph:
    return AuthorGraph(
        nodes=[
            WorkflowNode(id="input", node_type=NodeType.INPUT, title="Input"),
            WorkflowNode(
                id="write-docs",
                node_type=NodeType.AGENT_TASK,
                task_kind=TaskKind.DOCS,
                title="Write demo documentation",
                instruction="Create the exact documentation file in the sealed scope.",
                assigned_agent="mock",
                assignment_mode=AssignmentMode.LOCKED,
                new_files_candidate=["docs/agent-hub-demo.md"],
                risk_level_hint=RiskLevel.L1,
                requires_write=True,
            ),
            WorkflowNode(id="output", node_type=NodeType.OUTPUT, title="Output"),
        ],
        edges=[
            WorkflowEdge(id="edge-input-write", from_node="input", to_node="write-docs"),
            WorkflowEdge(id="edge-write-output", from_node="write-docs", to_node="output"),
        ],
    )
