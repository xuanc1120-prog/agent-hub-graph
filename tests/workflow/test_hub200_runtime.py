from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
from storage.db import Transaction
from storage.errors import (
    ChangeSetReconciliationRequired,
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


@pytest.mark.asyncio
async def test_capture_has_no_post_commit_read_before_task_terminal(
    fixture_source_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = WorkflowApplication(Settings(data_dir=tmp_path / "agent-hub-data"))
    await application.initialize()
    await application.register_mock_agent()
    session = await application.create_session(
        repo=fixture_source_repo,
        goal="Verify atomic write capture publication.",
        session_id="session-atomic-capture",
    )
    workflow = await application.services.workflows.create(
        NewWorkflow(
            workflow_id="workflow-atomic-capture",
            session_id=session.session_id,
            author_graph=_docs_write_graph(),
            layout=WorkflowLayout(),
        )
    )
    original_get = application.services.change_sets.get
    early_reads = 0

    async def reject_read_while_task_running(change_set_id: str):
        nonlocal early_reads
        async with application.services.database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT t.status
                FROM change_sets cs
                JOIN tasks t ON t.id = cs.task_id
                WHERE cs.id = ?
                """,
                (change_set_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is not None and row["status"] == TaskStatus.RUNNING.value:
            early_reads += 1
            raise RuntimeError("post-commit read observed a running write task")
        return await original_get(change_set_id)

    monkeypatch.setattr(
        application.services.change_sets,
        "get",
        reject_read_while_task_running,
    )

    async with application.temporary_master() as lease:
        run = await application.run(
            workflow.workflow_id,
            lease=lease,
            workflow_run_id="run-atomic-capture",
        )

    assert run.status == WorkflowRunStatus.BLOCKED
    assert early_reads == 0
    record = await original_get(
        (
            await application.services.change_sets.get_for_source_node(
                workflow_run_id=run.workflow_run_id,
                source_node_id="write-docs",
            )
        ).change_set.change_set_id
    )
    task = await application.services.runs.get_task(record.change_set.task_id)
    assert record.change_set.status == ChangeSetStatus.TEST_PASSED
    assert task.status == TaskStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_sensitive_ignored_baseline_blocks_agent_and_preimage_artifacts(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    (fixture_source_repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    subprocess.run(
        ["git", "-c", "core.autocrlf=false", "add", ".gitignore"],
        cwd=fixture_source_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "-c",
            "user.name=Agent Hub Tests",
            "-c",
            "user.email=tests@agent-hub.local",
            "commit",
            "-m",
            "ignore sensitive baseline",
        ],
        cwd=fixture_source_repo,
        check=True,
        capture_output=True,
    )
    application = WorkflowApplication(Settings(data_dir=tmp_path / "agent-hub-data"))
    await application.initialize()
    await application.register_mock_agent()
    session = await application.create_session(
        repo=fixture_source_repo,
        goal="Never expose an ignored secret.",
        session_id="session-sensitive-ignored",
    )
    (session.shared_repo_path / ".env").write_text(
        "DATABASE_URL=postgresql://secret@db/app\n",
        encoding="utf-8",
    )
    workflow = await application.services.workflows.create(
        NewWorkflow(
            workflow_id="workflow-sensitive-ignored",
            session_id=session.session_id,
            author_graph=_docs_write_graph(),
            layout=WorkflowLayout(),
        )
    )

    async with application.temporary_master() as lease:
        run = await application.run(
            workflow.workflow_id,
            lease=lease,
            workflow_run_id="run-sensitive-ignored",
        )

    assert run.status == WorkflowRunStatus.FAILED
    assert not (session.shared_repo_path / "docs" / "agent-hub-demo.md").exists()
    async with application.services.database.connection() as connection:
        change_count = await connection.execute("SELECT COUNT(*) FROM change_sets")
        assert (await change_count.fetchone())[0] == 0
        await change_count.close()
        preimage_count = await connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE artifact_type = 'change_preimage'"
        )
        assert (await preimage_count.fetchone())[0] == 0
        await preimage_count.close()


@pytest.mark.parametrize(
    "failure_point",
    ["cleanup", "output_artifact", "capture_artifact", "finish_task"],
)
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
    elif failure_point == "capture_artifact":
        original_stage_artifact = application.services.change_sets._stage_artifact

        def fail_capture_artifact_once(**kwargs: object):
            nonlocal injected
            if not injected and kwargs["artifact_type"] == ArtifactType.PATCH:
                injected = True
                raise RuntimeError("injected capture artifact failure")
            return original_stage_artifact(**kwargs)

        monkeypatch.setattr(
            application.services.change_sets,
            "_stage_artifact",
            fail_capture_artifact_once,
        )
    else:
        original_finish = application.services.change_sets._finish_task_in

        async def fail_success_finish_once(*args: object, **kwargs: object):
            nonlocal injected
            if not injected and kwargs["target"] == TaskStatus.SUCCEEDED:
                injected = True
                raise RuntimeError("injected task finalization failure")
            return await original_finish(*args, **kwargs)

        monkeypatch.setattr(
            application.services.change_sets,
            "_finish_task_in",
            fail_success_finish_once,
        )

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


@pytest.mark.parametrize(
    (
        "commit_persisted",
        "cancel_after_rollback",
        "cancel_during_reconciliation",
    ),
    [
        (False, False, False),
        (False, True, False),
        (True, False, False),
        (False, False, True),
        (True, False, True),
    ],
)
@pytest.mark.asyncio
async def test_capture_commit_exception_reconciles_durable_outcome(
    fixture_source_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commit_persisted: bool,
    cancel_after_rollback: bool,
    cancel_during_reconciliation: bool,
) -> None:
    application = WorkflowApplication(
        Settings(
            data_dir=tmp_path / "agent-hub-data",
            master_lease_ttl_seconds=300,
        )
    )
    await application.initialize()
    await application.register_mock_agent()
    case_id = (
        f"{int(commit_persisted)}-{int(cancel_after_rollback)}-{int(cancel_during_reconciliation)}"
    )
    session = await application.create_session(
        repo=fixture_source_repo,
        goal="Reconcile an ambiguous SQLite commit result.",
        session_id=f"session-commit-reconcile-{case_id}",
    )
    workflow = await application.services.workflows.create(
        NewWorkflow(
            workflow_id=f"workflow-commit-reconcile-{case_id}",
            session_id=session.session_id,
            author_graph=_docs_write_graph(),
            layout=WorkflowLayout(),
        )
    )
    database = application.services.database
    original_transaction = database.immediate_transaction
    original_persist = application.services.change_sets.persist_capture
    injected = False
    reconciliation_started = asyncio.Event()
    original_reconcile = application.services.change_sets._reconcile_capture_commit

    async def delayed_reconcile(**kwargs: object) -> str:
        reconciliation_started.set()
        await asyncio.sleep(0.05)
        return await original_reconcile(**kwargs)

    if cancel_during_reconciliation:
        monkeypatch.setattr(
            application.services.change_sets,
            "_reconcile_capture_commit",
            delayed_reconcile,
        )

    @asynccontextmanager
    async def faulting_transaction() -> AsyncIterator[Transaction]:
        async with database.connection() as connection:
            cursor = await connection.execute("BEGIN IMMEDIATE")
            await cursor.close()
            try:
                yield Transaction(connection)
            except BaseException:
                await connection.rollback()
                raise
            else:
                if commit_persisted:
                    await connection.commit()
                else:
                    await connection.rollback()
                    if cancel_after_rollback:
                        cursor = await connection.execute("BEGIN IMMEDIATE")
                        await cursor.close()
                        await connection.execute(
                            """
                            UPDATE workflow_runs
                            SET cancel_requested_at = strftime(
                                '%Y-%m-%dT%H:%M:%fZ',
                                'now'
                            )
                            WHERE id = ?
                            """,
                            (workflow_run_id,),
                        )
                        await connection.commit()
                raise RuntimeError("injected commit-path exception")

    async def persist_with_commit_fault(**kwargs: object):
        nonlocal injected
        if not injected and kwargs["status"] == ChangeSetStatus.CAPTURED:
            injected = True
            monkeypatch.setattr(
                database,
                "immediate_transaction",
                faulting_transaction,
            )
            try:
                return await original_persist(**kwargs)
            finally:
                monkeypatch.setattr(
                    database,
                    "immediate_transaction",
                    original_transaction,
                )
        return await original_persist(**kwargs)

    monkeypatch.setattr(
        application.services.change_sets,
        "persist_capture",
        persist_with_commit_fault,
    )

    workflow_run_id = f"run-commit-reconcile-{case_id}"
    async with application.temporary_master() as lease:
        if cancel_during_reconciliation:
            run_task = asyncio.create_task(
                application.run(
                    workflow.workflow_id,
                    lease=lease,
                    workflow_run_id=workflow_run_id,
                )
            )
            await asyncio.wait_for(reconciliation_started.wait(), timeout=30)
            run_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run_task
            run = await application.services.runs.get(workflow_run_id)
        else:
            run = await application.run(
                workflow.workflow_id,
                lease=lease,
                workflow_run_id=workflow_run_id,
            )

    assert injected is True
    record = await application.services.change_sets.get_for_source_node(
        workflow_run_id=run.workflow_run_id,
        source_node_id="write-docs",
    )
    assert record is not None
    task = await application.services.runs.get_task(record.change_set.task_id)
    if commit_persisted:
        if cancel_during_reconciliation:
            assert record.change_set.status == ChangeSetStatus.CAPTURED
        else:
            assert run.status == WorkflowRunStatus.BLOCKED
            assert record.change_set.status == ChangeSetStatus.TEST_PASSED
        assert task.status == TaskStatus.SUCCEEDED
    else:
        if not cancel_during_reconciliation:
            assert run.status == WorkflowRunStatus.FAILED
        assert record.change_set.status == ChangeSetStatus.ABANDONED_PARTIAL
        assert task.status == TaskStatus.FAILED

    capture_types = {
        ArtifactType.PATCH.value,
        ArtifactType.DIFF.value,
        ArtifactType.CHANGE_PREIMAGE.value,
    }
    task_artifacts = await application.services.artifacts.list_by_task(record.change_set.task_id)
    capture_artifacts = [
        artifact for artifact in task_artifacts if artifact.artifact_type in capture_types
    ]
    assert len(capture_artifacts) == 4
    expected_by_type: dict[str, set[str]] = {}
    for artifact in capture_artifacts:
        expected_by_type.setdefault(artifact.artifact_type, set()).add(artifact.artifact_id)
        await application.services.artifacts.get_and_verify(
            artifact.artifact_id,
            expected_session_id=session.session_id,
            expected_task_id=record.change_set.task_id,
        )
    for artifact_type in capture_types:
        directory = application.services.artifacts.store.base_dir / artifact_type
        actual = {path.name for path in directory.iterdir()} if directory.exists() else set()
        assert actual == expected_by_type.get(artifact_type, set())


@pytest.mark.parametrize(
    "fault_kind",
    [
        "cancel_requested_at",
        "duplicate_change_event",
        "old_event",
        "missing_finished_at",
        "missing_change_event",
        "missing_task_event",
        "next_event_seq_gap",
        "wrong_task_fencing_token",
    ],
)
@pytest.mark.asyncio
async def test_capture_commit_reconciliation_rejects_partial_durable_state(
    fixture_source_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_kind: str,
) -> None:
    application = WorkflowApplication(
        Settings(
            data_dir=tmp_path / "agent-hub-data",
            master_lease_ttl_seconds=300,
        )
    )
    await application.initialize()
    await application.register_mock_agent()
    session = await application.create_session(
        repo=fixture_source_repo,
        goal="Reject an incomplete durable capture transaction.",
        session_id=f"session-partial-commit-{fault_kind}",
    )
    workflow = await application.services.workflows.create(
        NewWorkflow(
            workflow_id=f"workflow-partial-commit-{fault_kind}",
            session_id=session.session_id,
            author_graph=_docs_write_graph(),
            layout=WorkflowLayout(),
        )
    )
    database = application.services.database
    original_transaction = database.immediate_transaction
    original_persist = application.services.change_sets.persist_capture
    injected = False
    active_task_id: str | None = None
    active_run_id: str | None = None

    @asynccontextmanager
    async def partially_committed_transaction() -> AsyncIterator[Transaction]:
        async with database.connection() as connection:
            cursor = await connection.execute("BEGIN IMMEDIATE")
            await cursor.close()
            try:
                yield Transaction(connection)
            except BaseException:
                await connection.rollback()
                raise
            else:
                assert active_task_id is not None
                assert active_run_id is not None
                await connection.commit()
                cursor = await connection.execute("BEGIN IMMEDIATE")
                await cursor.close()
                if fault_kind == "cancel_requested_at":
                    await connection.execute(
                        """
                        UPDATE workflow_runs
                        SET cancel_requested_at = (
                            SELECT finished_at FROM tasks WHERE id = ?
                        )
                        WHERE id = ?
                        """,
                        (active_task_id, active_run_id),
                    )
                elif fault_kind in {
                    "duplicate_change_event",
                    "old_event",
                }:
                    created_at = (
                        "2000-01-01T00:00:00.000000Z" if fault_kind == "old_event" else None
                    )
                    await connection.execute(
                        """
                        INSERT INTO events(
                            session_id, workflow_id, workflow_run_id, run_seq,
                            event_type, actor_type, actor_id, payload_json, created_at
                        )
                        SELECT e.session_id, e.workflow_id, e.workflow_run_id,
                               wr.next_event_seq, e.event_type, e.actor_type,
                               e.actor_id, e.payload_json,
                               COALESCE(?, e.created_at)
                        FROM events e
                        JOIN workflow_runs wr ON wr.id = e.workflow_run_id
                        WHERE e.workflow_run_id = ? AND e.event_type = ?
                          AND json_extract(e.payload_json, '$.task_id') = ?
                        ORDER BY e.run_seq DESC LIMIT 1
                        """,
                        (
                            created_at,
                            active_run_id,
                            "workflow.change_set_state_changed",
                            active_task_id,
                        ),
                    )
                    await connection.execute(
                        """
                        UPDATE workflow_runs SET next_event_seq = next_event_seq + 1
                        WHERE id = ?
                        """,
                        (active_run_id,),
                    )
                elif fault_kind == "missing_finished_at":
                    await connection.execute(
                        "UPDATE tasks SET finished_at = NULL WHERE id = ?",
                        (active_task_id,),
                    )
                elif fault_kind == "missing_change_event":
                    await connection.execute(
                        """
                        DELETE FROM events
                        WHERE workflow_run_id = ? AND event_type = ?
                          AND json_extract(payload_json, '$.task_id') = ?
                        """,
                        (
                            active_run_id,
                            "workflow.change_set_state_changed",
                            active_task_id,
                        ),
                    )
                elif fault_kind == "missing_task_event":
                    await connection.execute(
                        """
                        DELETE FROM events
                        WHERE workflow_run_id = ? AND event_type = ?
                          AND json_extract(payload_json, '$.task_id') = ?
                        """,
                        (
                            active_run_id,
                            "workflow.task_state_changed",
                            active_task_id,
                        ),
                    )
                elif fault_kind == "next_event_seq_gap":
                    await connection.execute(
                        "UPDATE workflow_runs SET next_event_seq = next_event_seq + 1 WHERE id = ?",
                        (active_run_id,),
                    )
                else:
                    await connection.execute(
                        """
                        UPDATE events
                        SET payload_json = json_set(
                            payload_json,
                            '$.workspace_fencing_token',
                            2147483647
                        )
                        WHERE workflow_run_id = ? AND event_type = ?
                          AND json_extract(payload_json, '$.task_id') = ?
                        """,
                        (
                            active_run_id,
                            "workflow.task_state_changed",
                            active_task_id,
                        ),
                    )
                await connection.commit()
                raise RuntimeError("injected partial commit-path exception")

    async def persist_with_partial_commit(**kwargs: object):
        nonlocal active_run_id, active_task_id, injected
        if not injected and kwargs["status"] == ChangeSetStatus.CAPTURED:
            injected = True
            active_task_id = str(kwargs["task_id"])
            active_run_id = str(kwargs["workflow_run_id"])
            monkeypatch.setattr(
                database,
                "immediate_transaction",
                partially_committed_transaction,
            )
            try:
                return await original_persist(**kwargs)
            finally:
                monkeypatch.setattr(
                    database,
                    "immediate_transaction",
                    original_transaction,
                )
        return await original_persist(**kwargs)

    monkeypatch.setattr(
        application.services.change_sets,
        "persist_capture",
        persist_with_partial_commit,
    )

    async with application.temporary_master() as lease:
        with pytest.raises(ChangeSetReconciliationRequired):
            await application.run(
                workflow.workflow_id,
                lease=lease,
                workflow_run_id=f"run-partial-commit-{fault_kind}",
            )

    assert injected is True
    assert application.services.git.state(session.shared_repo_path).dirty is False
    assert not (session.shared_repo_path / "docs" / "agent-hub-demo.md").exists()
    async with database.connection() as connection:
        task = await connection.execute(
            "SELECT status, finished_at FROM tasks WHERE id = ?",
            (active_task_id,),
        )
        task_row = await task.fetchone()
        await task.close()
        node = await connection.execute(
            "SELECT status, outcome, finished_at FROM node_runs WHERE workflow_run_id = ? "
            "AND node_id = 'write-docs'",
            (active_run_id,),
        )
        node_row = await node.fetchone()
        await node.close()
    assert task_row is not None and task_row["status"] == TaskStatus.SUCCEEDED.value
    assert node_row is not None and node_row["status"] == NodeRunStatus.RUNNING.value
    assert node_row["outcome"] is None
    assert node_row["finished_at"] is None


@pytest.mark.asyncio
async def test_abandoned_capture_reconciliation_requires_security_event(
    fixture_source_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = WorkflowApplication(
        Settings(
            data_dir=tmp_path / "agent-hub-data",
            master_lease_ttl_seconds=300,
        )
    )
    await application.initialize()
    await application.register_mock_agent()
    session = await application.create_session(
        repo=fixture_source_repo,
        goal="Require a complete audit trail for abandoned capture.",
        session_id="session-abandoned-security-reconcile",
    )
    workflow = await application.services.workflows.create(
        NewWorkflow(
            workflow_id="workflow-abandoned-security-reconcile",
            session_id=session.session_id,
            author_graph=_docs_write_graph(),
            layout=WorkflowLayout(),
        )
    )
    agent_handler = application.services.registry.get_handler(_docs_write_graph().nodes[1])
    original_cleanup = agent_handler._bundles.cleanup
    cleanup_failed = False

    async def fail_cleanup_once(task_id: str) -> CleanupResult:
        nonlocal cleanup_failed
        if not cleanup_failed:
            cleanup_failed = True
            return CleanupResult(removed_files=0, removed_dirs=0, errors=["injected"])
        return await original_cleanup(task_id)

    monkeypatch.setattr(agent_handler._bundles, "cleanup", fail_cleanup_once)
    database = application.services.database
    original_transaction = database.immediate_transaction
    original_persist = application.services.change_sets.persist_capture
    reconciliation_injected = False
    active_task_id: str | None = None
    active_run_id: str | None = None

    @asynccontextmanager
    async def missing_security_event_transaction() -> AsyncIterator[Transaction]:
        async with database.connection() as connection:
            cursor = await connection.execute("BEGIN IMMEDIATE")
            await cursor.close()
            try:
                yield Transaction(connection)
            except BaseException:
                await connection.rollback()
                raise
            else:
                assert active_task_id is not None
                assert active_run_id is not None
                await connection.commit()
                cursor = await connection.execute("BEGIN IMMEDIATE")
                await cursor.close()
                await connection.execute(
                    """
                    DELETE FROM security_events
                    WHERE workflow_run_id = ? AND task_id = ?
                      AND event_type = 'changeset.state_rejected'
                    """,
                    (active_run_id, active_task_id),
                )
                await connection.commit()
                raise RuntimeError("injected missing security event")

    async def persist_without_security_event(**kwargs: object):
        nonlocal active_run_id, active_task_id, reconciliation_injected
        if not reconciliation_injected and kwargs["status"] == ChangeSetStatus.ABANDONED_PARTIAL:
            reconciliation_injected = True
            active_task_id = str(kwargs["task_id"])
            active_run_id = str(kwargs["workflow_run_id"])
            monkeypatch.setattr(
                database,
                "immediate_transaction",
                missing_security_event_transaction,
            )
            try:
                return await original_persist(**kwargs)
            finally:
                monkeypatch.setattr(
                    database,
                    "immediate_transaction",
                    original_transaction,
                )
        return await original_persist(**kwargs)

    monkeypatch.setattr(
        application.services.change_sets,
        "persist_capture",
        persist_without_security_event,
    )

    async with application.temporary_master() as lease:
        with pytest.raises(ChangeSetReconciliationRequired):
            await application.run(
                workflow.workflow_id,
                lease=lease,
                workflow_run_id="run-abandoned-security-reconcile",
            )

    assert cleanup_failed is True
    assert reconciliation_injected is True
    assert application.services.git.state(session.shared_repo_path).dirty is False
    assert not (session.shared_repo_path / "docs" / "agent-hub-demo.md").exists()
    async with database.connection() as connection:
        task = await connection.execute(
            "SELECT status, finished_at FROM tasks WHERE id = ?",
            (active_task_id,),
        )
        task_row = await task.fetchone()
        await task.close()
        change = await connection.execute(
            "SELECT status FROM change_sets WHERE task_id = ?",
            (active_task_id,),
        )
        change_row = await change.fetchone()
        await change.close()
        node = await connection.execute(
            "SELECT status, outcome, finished_at FROM node_runs WHERE workflow_run_id = ? "
            "AND node_id = 'write-docs'",
            (active_run_id,),
        )
        node_row = await node.fetchone()
        await node.close()
        security = await connection.execute(
            "SELECT COUNT(*) FROM security_events WHERE workflow_run_id = ? AND task_id = ?",
            (active_run_id, active_task_id),
        )
        security_count = int((await security.fetchone())[0])
        await security.close()
    assert task_row is not None and task_row["status"] == TaskStatus.FAILED.value
    assert task_row["finished_at"] is not None
    assert change_row is not None
    assert change_row["status"] == ChangeSetStatus.ABANDONED_PARTIAL.value
    assert node_row is not None and node_row["status"] == NodeRunStatus.RUNNING.value
    assert node_row["outcome"] is None
    assert node_row["finished_at"] is None
    assert security_count == 0


@pytest.mark.asyncio
async def test_lease_loss_after_restore_never_publishes_live_capture(
    fixture_source_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = WorkflowApplication(Settings(data_dir=tmp_path / "agent-hub-data"))
    await application.initialize()
    await application.register_mock_agent()
    session = await application.create_session(
        repo=fixture_source_repo,
        goal="Lose the lease after capture and before publication.",
        session_id="session-lease-loss-after-capture",
    )
    workflow = await application.services.workflows.create(
        NewWorkflow(
            workflow_id="workflow-lease-loss-after-capture",
            session_id=session.session_id,
            author_graph=_docs_write_graph(),
            layout=WorkflowLayout(),
        )
    )
    original_persist = application.services.change_sets.persist_capture
    replacement = None
    injected = False

    async def lose_success_publication(**kwargs: object):
        nonlocal injected, replacement
        if not injected and kwargs["status"] == ChangeSetStatus.CAPTURED:
            injected = True
            old_lease = kwargs["workspace_lease"]
            await application.services.locks.release(old_lease)
            replacement = await application.services.locks.acquire(
                session_id=session.session_id,
                owner_kind=WorkspaceOwnerKind.RECOVERY,
                owner_operation_id="recovery-replaces-agent-task",
                owner_process_id=os.getpid(),
                ttl_seconds=30,
            )
        return await original_persist(**kwargs)

    monkeypatch.setattr(
        application.services.change_sets,
        "persist_capture",
        lose_success_publication,
    )

    try:
        async with application.temporary_master() as lease:
            with pytest.raises(LeaseLost):
                await application.run(
                    workflow.workflow_id,
                    lease=lease,
                    workflow_run_id="run-lease-loss-after-capture",
                )
    finally:
        if replacement is not None:
            await application.services.locks.release(replacement)

    assert injected is True
    run = await application.services.runs.get("run-lease-loss-after-capture")
    assert run.status == WorkflowRunStatus.RUNNING
    node_runs = await application.services.runs.list_nodes(run.workflow_run_id)
    write_node = next(node for node in node_runs if node.node_id == "write-docs")
    assert write_node.status == NodeRunStatus.RUNNING
    async with application.services.database.connection() as connection:
        task_cursor = await connection.execute(
            "SELECT status FROM tasks WHERE node_run_id = ?",
            (write_node.node_run_id,),
        )
        task = await task_cursor.fetchone()
        await task_cursor.close()
        change_cursor = await connection.execute("SELECT COUNT(*) FROM change_sets")
        change_count = await change_cursor.fetchone()
        await change_cursor.close()
        artifact_cursor = await connection.execute(
            "SELECT COUNT(*) FROM artifacts "
            "WHERE artifact_type IN ('patch', 'diff', 'change_preimage')"
        )
        capture_artifact_count = await artifact_cursor.fetchone()
        await artifact_cursor.close()
    assert task is not None and task["status"] == TaskStatus.RUNNING.value
    assert change_count is not None and change_count[0] == 0
    assert capture_artifact_count is not None and capture_artifact_count[0] == 0
    for artifact_type in (
        ArtifactType.PATCH,
        ArtifactType.DIFF,
        ArtifactType.CHANGE_PREIMAGE,
    ):
        artifact_directory = application.services.artifacts.store.base_dir / artifact_type.value
        if artifact_directory.exists():
            assert list(artifact_directory.iterdir()) == []
    assert application.services.git.state(session.shared_repo_path).dirty is False
    assert not (session.shared_repo_path / "docs" / "agent-hub-demo.md").exists()


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

    fenced_calls = 0

    async def lose_before_capture(*args: object, **kwargs: object) -> None:
        nonlocal fenced_calls
        _ = args, kwargs
        fenced_calls += 1
        raise LeaseLost("injected workspace lease loss")

    monkeypatch.setattr(application.services.locks, "run_fenced", lose_before_capture)

    async with application.temporary_master() as lease:
        with pytest.raises(LeaseLost, match="injected workspace lease loss"):
            await application.run(
                workflow.workflow_id,
                lease=lease,
                workflow_run_id="run-write-lease-lost",
            )

    assert fenced_calls == 1
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
    assert application.services.git.state(session.shared_repo_path).dirty is False
    assert not (session.shared_repo_path / "docs" / "agent-hub-demo.md").exists()


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
