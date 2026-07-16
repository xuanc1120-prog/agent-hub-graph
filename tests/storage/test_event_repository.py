"""Tests for storage.event_repository — typed append, run_seq, bounded reads."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import Field

from protocol import ActorType, AuthorGraph, StrictModel, WorkflowLayout
from protocol.workflow import WorkflowNode
from storage.db import Database, utc_now_text
from storage.errors import EventPayloadError, RecordNotFound
from storage.event_registry import EventRegistry
from storage.event_repository import EventRecord, EventRepository
from storage.repositories import (
    NewSession,
    NewWorkflow,
    SessionRepository,
    WorkflowRepository,
)


class TaskStartedPayload(StrictModel):
    task_id: str = Field(max_length=128)
    agent_id: str = Field(max_length=128)


class TaskCompletedPayload(StrictModel):
    task_id: str = Field(max_length=128)
    outcome: str = Field(max_length=50)


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "data" / "agent-hub.db")
    await db.initialize()
    return db


@pytest.fixture
def registry() -> EventRegistry:
    reg = EventRegistry()
    reg.register("task.started", TaskStartedPayload)
    reg.register("task.completed", TaskCompletedPayload)
    return reg


@pytest_asyncio.fixture
async def event_repo(database: Database, registry: EventRegistry) -> EventRepository:
    return EventRepository(database, registry)


async def _seed_session_and_workflow(database: Database, session_id: str = "sess-001") -> None:
    """Create session, workflow, and workflow_run for testing."""
    session_repo = SessionRepository(database)
    await session_repo.create(
        NewSession(
            session_id=session_id,
            goal="test goal",
            source_repo_path=Path("/tmp/source"),
            shared_repo_path=Path(f"/tmp/shared-{session_id}"),
            base_commit="a" * 40,
            integration_branch="main",
            integration_head_commit="b" * 40,
        )
    )
    wf_repo = WorkflowRepository(database)
    author = AuthorGraph(
        nodes=[WorkflowNode(id="n1", node_type="input", title="Start")],
        edges=[],
    )
    layout = WorkflowLayout(nodes=[])
    await wf_repo.create(
        NewWorkflow(
            workflow_id="wf-001",
            session_id=session_id,
            author_graph=author,
            layout=layout,
        )
    )
    # Create workflow_run directly with correct column count
    timestamp = utc_now_text()
    async with database.immediate_transaction() as tx:
        await tx.execute(
            """
            INSERT INTO workflow_runs(
                id, workflow_id, session_id, integration_base_commit,
                current_commit, workflow_semantic_version,
                workflow_layout_version,
                author_snapshot_json, author_snapshot_hash,
                compiled_snapshot_json, compiled_snapshot_hash,
                layout_snapshot_json, layout_snapshot_hash,
                policy_version, agent_catalog_snapshot_json,
                agent_catalog_snapshot_hash, status,
                next_event_seq, created_at
            ) VALUES (
                ?, ?, ?, ?, ?, 1, 1,
                '{}', ?, '{}', ?, '{}', ?,
                '1', '{}', ?, 'running', 1, ?
            )
            """,
            (
                "run-001",  # id
                "wf-001",  # workflow_id
                session_id,  # session_id
                "a" * 40,  # integration_base_commit
                "b" * 40,  # current_commit
                "c" * 64,  # author_snapshot_hash
                "d" * 64,  # compiled_snapshot_hash
                "e" * 64,  # layout_snapshot_hash
                "f" * 64,  # agent_catalog_snapshot_hash
                timestamp,  # created_at
            ),
        )


# --- Non-run events --------------------------------------------------------


class TestNonRunEvents:
    @pytest.mark.asyncio
    async def test_append_non_run_event(
        self,
        event_repo: EventRepository,
        database: Database,
    ) -> None:
        await _seed_session_and_workflow(database)
        record = await event_repo.append(
            session_id="sess-001",
            event_type="task.started",
            actor_type=ActorType.MASTER,
            actor_id="master-001",
            payload=TaskStartedPayload(task_id="t-001", agent_id="opencode"),
        )
        assert record.event_id > 0
        assert record.run_seq is None
        assert record.workflow_id is None
        assert record.workflow_run_id is None
        assert record.event_type == "task.started"

    @pytest.mark.asyncio
    async def test_non_run_event_with_workflow_id_allowed(
        self,
        event_repo: EventRepository,
        database: Database,
    ) -> None:
        """Frozen EventEnvelope allows workflow-level non-run events."""
        await _seed_session_and_workflow(database)
        record = await event_repo.append(
            session_id="sess-001",
            event_type="task.started",
            actor_type=ActorType.MASTER,
            actor_id=None,
            payload=TaskStartedPayload(task_id="t-001", agent_id="opencode"),
            workflow_id="wf-001",
        )
        assert record.workflow_id == "wf-001"
        assert record.workflow_run_id is None
        assert record.run_seq is None

    @pytest.mark.asyncio
    async def test_non_run_event_without_session_fails(self, event_repo: EventRepository) -> None:
        with pytest.raises(RecordNotFound, match="session not found"):
            await event_repo.append(
                session_id="nonexistent",
                event_type="task.started",
                actor_type=ActorType.MASTER,
                actor_id=None,
                payload=TaskStartedPayload(task_id="t-001", agent_id="opencode"),
            )

    @pytest.mark.asyncio
    async def test_typed_payload_enforced(
        self,
        event_repo: EventRepository,
        database: Database,
    ) -> None:
        """Repository rejects wrong typed payload."""
        await _seed_session_and_workflow(database)
        with pytest.raises(EventPayloadError, match="must be a"):
            await event_repo.append(
                session_id="sess-001",
                event_type="task.started",
                actor_type=ActorType.MASTER,
                actor_id=None,
                payload=TaskCompletedPayload(task_id="t-001", outcome="ok"),
            )


# --- Run events with atomic seq allocation ---------------------------------


class TestRunEvents:
    @pytest.mark.asyncio
    async def test_append_run_event_allocates_seq(
        self,
        event_repo: EventRepository,
        database: Database,
    ) -> None:
        await _seed_session_and_workflow(database)
        record = await event_repo.append(
            session_id="sess-001",
            event_type="task.started",
            actor_type=ActorType.AGENT,
            actor_id="opencode-001",
            payload=TaskStartedPayload(task_id="t-001", agent_id="opencode"),
            workflow_id="wf-001",
            workflow_run_id="run-001",
        )
        assert record.run_seq == 1
        assert record.workflow_id == "wf-001"
        assert record.workflow_run_id == "run-001"

    @pytest.mark.asyncio
    async def test_sequential_run_events_have_incrementing_seq(
        self,
        event_repo: EventRepository,
        database: Database,
    ) -> None:
        await _seed_session_and_workflow(database)
        records = []
        for i in range(5):
            r = await event_repo.append(
                session_id="sess-001",
                event_type="task.started",
                actor_type=ActorType.AGENT,
                actor_id=f"agent-{i}",
                payload=TaskStartedPayload(task_id=f"t-{i}", agent_id="opencode"),
                workflow_id="wf-001",
                workflow_run_id="run-001",
            )
            records.append(r)
        seqs = [r.run_seq for r in records]
        assert seqs == [1, 2, 3, 4, 5]

    @pytest.mark.asyncio
    async def test_concurrent_run_events_unique_seq(
        self,
        database: Database,
        registry: EventRegistry,
    ) -> None:
        """Concurrent appenders must get unique, strictly increasing seqs."""
        await _seed_session_and_workflow(database)
        repo = EventRepository(database, registry)

        async def append_event(idx: int) -> EventRecord:
            return await repo.append(
                session_id="sess-001",
                event_type="task.started",
                actor_type=ActorType.AGENT,
                actor_id=f"agent-{idx}",
                payload=TaskStartedPayload(task_id=f"t-{idx}", agent_id="opencode"),
                workflow_id="wf-001",
                workflow_run_id="run-001",
            )

        results = await asyncio.gather(*[append_event(i) for i in range(10)])
        seqs = sorted(r.run_seq for r in results)
        assert seqs == list(range(1, 11))
        assert len(set(seqs)) == 10

    @pytest.mark.asyncio
    async def test_run_event_without_workflow_id_rejected(
        self,
        event_repo: EventRepository,
        database: Database,
    ) -> None:
        await _seed_session_and_workflow(database)
        with pytest.raises(EventPayloadError, match="must carry workflow_id"):
            await event_repo.append(
                session_id="sess-001",
                event_type="task.started",
                actor_type=ActorType.AGENT,
                actor_id=None,
                payload=TaskStartedPayload(task_id="t-001", agent_id="opencode"),
                workflow_run_id="run-001",
            )

    @pytest.mark.asyncio
    async def test_run_event_wrong_workflow_rejected(
        self,
        event_repo: EventRepository,
        database: Database,
    ) -> None:
        await _seed_session_and_workflow(database)
        with pytest.raises(RecordNotFound, match="workflow not found"):
            await event_repo.append(
                session_id="sess-001",
                event_type="task.started",
                actor_type=ActorType.AGENT,
                actor_id=None,
                payload=TaskStartedPayload(task_id="t-001", agent_id="opencode"),
                workflow_id="wf-nonexistent",
                workflow_run_id="run-001",
            )


# --- Bounded reads ---------------------------------------------------------


class TestBoundedReads:
    @pytest.mark.asyncio
    async def test_list_by_session(
        self,
        event_repo: EventRepository,
        database: Database,
    ) -> None:
        await _seed_session_and_workflow(database)
        for i in range(3):
            await event_repo.append(
                session_id="sess-001",
                event_type="task.started",
                actor_type=ActorType.MASTER,
                actor_id=None,
                payload=TaskStartedPayload(task_id=f"t-{i}", agent_id="opencode"),
            )
        events = await event_repo.list_by_session("sess-001")
        assert len(events) == 3

    @pytest.mark.asyncio
    async def test_list_by_session_pagination(
        self,
        event_repo: EventRepository,
        database: Database,
    ) -> None:
        await _seed_session_and_workflow(database)
        for i in range(5):
            await event_repo.append(
                session_id="sess-001",
                event_type="task.started",
                actor_type=ActorType.MASTER,
                actor_id=None,
                payload=TaskStartedPayload(task_id=f"t-{i}", agent_id="opencode"),
            )
        first_page = await event_repo.list_by_session("sess-001", limit=2)
        assert len(first_page) == 2
        second_page = await event_repo.list_by_session(
            "sess-001",
            after_event_id=first_page[-1].event_id,
            limit=2,
        )
        assert len(second_page) == 2
        assert second_page[0].event_id > first_page[-1].event_id

    @pytest.mark.asyncio
    async def test_list_by_run(
        self,
        event_repo: EventRepository,
        database: Database,
    ) -> None:
        await _seed_session_and_workflow(database)
        for i in range(3):
            await event_repo.append(
                session_id="sess-001",
                event_type="task.started",
                actor_type=ActorType.AGENT,
                actor_id=f"agent-{i}",
                payload=TaskStartedPayload(task_id=f"t-{i}", agent_id="opencode"),
                workflow_id="wf-001",
                workflow_run_id="run-001",
            )
        events = await event_repo.list_by_run("run-001")
        assert len(events) == 3
        assert all(e.run_seq is not None for e in events)
        assert events[0].run_seq < events[1].run_seq < events[2].run_seq

    @pytest.mark.asyncio
    async def test_negative_limit_rejected(self, event_repo: EventRepository) -> None:
        with pytest.raises(ValueError, match="must be >= 1"):
            await event_repo.list_by_session("sess-001", limit=-1)

    @pytest.mark.asyncio
    async def test_zero_limit_rejected(self, event_repo: EventRepository) -> None:
        with pytest.raises(ValueError, match="must be >= 1"):
            await event_repo.list_by_run("run-001", limit=0)


# --- Get by ID and revalidation -------------------------------------------


class TestGetById:
    @pytest.mark.asyncio
    async def test_get_existing_event(
        self,
        event_repo: EventRepository,
        database: Database,
    ) -> None:
        await _seed_session_and_workflow(database)
        created = await event_repo.append(
            session_id="sess-001",
            event_type="task.started",
            actor_type=ActorType.MASTER,
            actor_id=None,
            payload=TaskStartedPayload(task_id="t-001", agent_id="opencode"),
        )
        fetched = await event_repo.get_by_id(created.event_id)
        assert fetched.event_id == created.event_id
        assert fetched.event_type == "task.started"

    @pytest.mark.asyncio
    async def test_get_nonexistent_event(self, event_repo: EventRepository) -> None:
        with pytest.raises(RecordNotFound):
            await event_repo.get_by_id(999999)

    @pytest.mark.asyncio
    async def test_list_validates_through_registry(
        self,
        event_repo: EventRepository,
        database: Database,
    ) -> None:
        """list_by_session validates payload through registry."""
        await _seed_session_and_workflow(database)
        await event_repo.append(
            session_id="sess-001",
            event_type="task.started",
            actor_type=ActorType.MASTER,
            actor_id=None,
            payload=TaskStartedPayload(task_id="t-001", agent_id="opencode"),
        )
        events = await event_repo.list_by_session("sess-001")
        assert len(events) == 1
        assert events[0].event_type == "task.started"
