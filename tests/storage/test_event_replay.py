from __future__ import annotations

from pathlib import Path

from protocol import ActorType, AuthorGraph, StrictModel, WorkflowLayout
from storage.db import Database, utc_now_text
from storage.event_registry import EventRegistry
from storage.event_repository import EventRepository
from storage.repositories import (
    NewSession,
    NewWorkflow,
    SessionRepository,
    WorkflowRepository,
)


class _ReplayPayload(StrictModel):
    ordinal: int


async def _seed_run(database: Database) -> None:
    await SessionRepository(database).create(
        NewSession(
            session_id="session-replay",
            goal="Replay every event.",
            source_repo_path=Path("/tmp/source"),
            shared_repo_path=Path("/tmp/shared-replay"),
            base_commit="a" * 40,
            integration_branch="main",
            integration_head_commit="b" * 40,
        )
    )
    await WorkflowRepository(database).create(
        NewWorkflow(
            workflow_id="workflow-replay",
            session_id="session-replay",
            author_graph=AuthorGraph(),
            layout=WorkflowLayout(),
        )
    )
    timestamp = utc_now_text()
    async with database.immediate_transaction() as transaction:
        await transaction.execute(
            """
            INSERT INTO workflow_runs(
                id, workflow_id, session_id, integration_base_commit,
                current_commit, workflow_semantic_version,
                workflow_layout_version, author_snapshot_json,
                author_snapshot_hash, compiled_snapshot_json,
                compiled_snapshot_hash, layout_snapshot_json,
                layout_snapshot_hash, policy_version,
                agent_catalog_snapshot_json, agent_catalog_snapshot_hash,
                status, next_event_seq, created_at
            ) VALUES (
                'run-replay', 'workflow-replay', 'session-replay', ?, ?,
                1, 1, '{}', ?, '{}', ?, '{}', ?, '1', '{}', ?,
                'running', 1, ?
            )
            """,
            (
                "a" * 40,
                "b" * 40,
                "c" * 64,
                "d" * 64,
                "e" * 64,
                "f" * 64,
                timestamp,
            ),
        )


async def test_full_run_replay_crosses_repository_page_limit(
    database: Database,
) -> None:
    await _seed_run(database)
    registry = EventRegistry()
    registry.register("replay.probe", _ReplayPayload)
    repository = EventRepository(database, registry)
    async with database.immediate_transaction() as transaction:
        for ordinal in range(1, 604):
            await repository.append_in(
                transaction,
                session_id="session-replay",
                workflow_id="workflow-replay",
                workflow_run_id="run-replay",
                event_type="replay.probe",
                actor_type=ActorType.MASTER,
                actor_id="master-replay",
                payload=_ReplayPayload(ordinal=ordinal),
            )

    events = await repository.list_all_by_run("run-replay", page_size=37)
    session_events = await repository.list_all_by_session("session-replay", page_size=41)

    assert len(events) == 603
    assert [event.run_seq for event in events] == list(range(1, 604))
    assert len({event.event_id for event in events}) == 603
    assert events[-1].run_seq == 603
    assert [event.event_id for event in session_events] == [event.event_id for event in events]
