from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from protocol import AuthorGraph, NodePosition, SessionStatus, WorkflowLayout, WorkflowNode
from storage.db import Database
from storage.errors import ConcurrencyConflict
from storage.repositories import (
    NewSession,
    NewWorkflow,
    SessionRepository,
    WorkflowRepository,
)

NOW = datetime(2026, 7, 12, tzinfo=UTC)


def _new_session(tmp_path: Path, session_id: str = "session-1") -> NewSession:
    return NewSession(
        session_id=session_id,
        goal="Implement deterministic storage",
        source_repo_path=tmp_path / f"{session_id}-source",
        shared_repo_path=tmp_path / f"{session_id}-shared",
        base_commit="a" * 40,
        integration_branch="agent-hub/integration",
        integration_head_commit="a" * 40,
    )


async def test_session_status_transition_is_compare_and_swap(
    database: Database, tmp_path: Path
) -> None:
    repository = SessionRepository(database)
    created = await repository.create(_new_session(tmp_path), now=NOW)
    assert created.status is SessionStatus.ACTIVE

    blocked = await repository.transition_status(
        created.session_id,
        expected=SessionStatus.ACTIVE,
        target=SessionStatus.BLOCKED,
        now=NOW,
    )
    assert blocked.status is SessionStatus.BLOCKED

    with pytest.raises(ConcurrencyConflict):
        await repository.transition_status(
            created.session_id,
            expected=SessionStatus.ACTIVE,
            target=SessionStatus.ARCHIVED,
            now=NOW,
        )
    assert (await repository.get(created.session_id)).status is SessionStatus.BLOCKED


async def test_workflow_semantic_and_layout_versions_are_independent(
    database: Database, tmp_path: Path
) -> None:
    sessions = SessionRepository(database)
    await sessions.create(_new_session(tmp_path), now=NOW)
    workflows = WorkflowRepository(database)
    created = await workflows.create(
        NewWorkflow(
            workflow_id="workflow-1",
            session_id="session-1",
            author_graph=AuthorGraph(),
            layout=WorkflowLayout(),
        ),
        now=NOW,
    )
    assert (created.semantic_version, created.layout_version) == (1, 1)

    graph = AuthorGraph(
        nodes=[WorkflowNode(id="input", node_type="input", title="Input")],
    )
    semantic = await workflows.update_author_graph(
        created.workflow_id,
        graph,
        expected_semantic_version=1,
        now=NOW,
    )
    assert (semantic.semantic_version, semantic.layout_version) == (2, 1)
    assert semantic.author_graph == graph

    layout = WorkflowLayout(nodes=[{"node_id": "input", "position": NodePosition(x=10, y=20)}])
    positioned = await workflows.update_layout(
        created.workflow_id,
        layout,
        expected_layout_version=1,
        now=NOW,
    )
    assert (positioned.semantic_version, positioned.layout_version) == (2, 2)
    assert positioned.layout == layout

    with pytest.raises(ConcurrencyConflict):
        await workflows.update_author_graph(
            created.workflow_id,
            AuthorGraph(),
            expected_semantic_version=1,
            now=NOW,
        )
    with pytest.raises(ConcurrencyConflict):
        await workflows.update_layout(
            created.workflow_id,
            WorkflowLayout(),
            expected_layout_version=1,
            now=NOW,
        )
