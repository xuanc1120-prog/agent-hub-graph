"""Typed repositories with explicit compare-and-swap operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import aiosqlite
from pydantic import TypeAdapter

from protocol import (
    AuthorGraph,
    EntityId,
    GitObjectId,
    SessionStatus,
    Sha256Hex,
    WorkflowLayout,
    canonical_json,
)
from storage.db import Database, Transaction, utc_now_text
from storage.errors import ConcurrencyConflict, RecordNotFound

_ENTITY_ID = TypeAdapter(EntityId)
_GIT_OBJECT_ID = TypeAdapter(GitObjectId)
_SHA256 = TypeAdapter(Sha256Hex)


def _entity_id(value: str) -> str:
    return _ENTITY_ID.validate_python(value)


def _git_object_id(value: str) -> str:
    return _GIT_OBJECT_ID.validate_python(value)


def _sha256(value: str) -> str:
    return _SHA256.validate_python(value)


def _model_hash(model: AuthorGraph | WorkflowLayout) -> str:
    return sha256(canonical_json(model)).hexdigest()


@dataclass(frozen=True, slots=True)
class NewSession:
    session_id: str
    goal: str
    source_repo_path: Path
    shared_repo_path: Path
    base_commit: str
    integration_branch: str
    integration_head_commit: str
    status: SessionStatus = SessionStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    goal: str
    source_repo_path: Path
    shared_repo_path: Path
    base_commit: str
    integration_branch: str
    integration_head_commit: str
    status: SessionStatus
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class NewWorkflow:
    workflow_id: str
    session_id: str
    author_graph: AuthorGraph
    layout: WorkflowLayout
    parent_workflow_id: str | None = None
    source_planner_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowRecord:
    workflow_id: str
    session_id: str
    parent_workflow_id: str | None
    source_planner_run_id: str | None
    semantic_version: int
    layout_version: int
    author_graph: AuthorGraph
    author_graph_hash: str
    layout: WorkflowLayout
    layout_hash: str
    created_at: str
    updated_at: str


async def _fetchone(
    transaction: Transaction,
    query: str,
    parameters: tuple[object, ...],
) -> aiosqlite.Row | None:
    return await transaction.fetch_one(query, parameters)


class SessionRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(self, value: NewSession, *, now: datetime | None = None) -> SessionRecord:
        async with self._database.immediate_transaction() as transaction:
            return await self.create_in(transaction, value, now=now)

    async def create_in(
        self,
        transaction: Transaction,
        value: NewSession,
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        session_id = _entity_id(value.session_id)
        if not value.goal or len(value.goal) > 20_000:
            raise ValueError("goal must contain 1..20000 characters")
        base_commit = _git_object_id(value.base_commit)
        integration_head = _git_object_id(value.integration_head_commit)
        if not value.integration_branch or len(value.integration_branch) > 500:
            raise ValueError("integration_branch must contain 1..500 characters")
        timestamp = utc_now_text(now)
        source_path = str(value.source_repo_path.expanduser().resolve(strict=False))
        shared_path = str(value.shared_repo_path.expanduser().resolve(strict=False))

        await transaction.execute(
            """
            INSERT INTO sessions(
                id, goal, source_repo_path, shared_repo_path, base_commit,
                integration_branch, integration_head_commit, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                value.goal,
                source_path,
                shared_path,
                base_commit,
                value.integration_branch,
                integration_head,
                value.status.value,
                timestamp,
                timestamp,
            ),
        )
        row = await _fetchone(transaction, "SELECT * FROM sessions WHERE id = ?", (session_id,))
        assert row is not None
        return self._to_record(row)

    async def get(self, session_id: str) -> SessionRecord:
        resolved_id = _entity_id(session_id)
        async with self._database.connection() as connection:
            row = await _fetchone(
                Transaction(connection), "SELECT * FROM sessions WHERE id = ?", (resolved_id,)
            )
        if row is None:
            raise RecordNotFound(f"session not found: {resolved_id}")
        return self._to_record(row)

    async def transition_status(
        self,
        session_id: str,
        *,
        expected: SessionStatus,
        target: SessionStatus,
        now: datetime | None = None,
    ) -> SessionRecord:
        resolved_id = _entity_id(session_id)
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            changed = await transaction.execute(
                """
                UPDATE sessions
                SET status = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (target.value, timestamp, resolved_id, expected.value),
            )
            if changed != 1:
                exists = await _fetchone(
                    transaction, "SELECT status FROM sessions WHERE id = ?", (resolved_id,)
                )
                if exists is None:
                    raise RecordNotFound(f"session not found: {resolved_id}")
                raise ConcurrencyConflict(
                    f"session {resolved_id} expected {expected.value}, found {exists['status']}"
                )
            row = await _fetchone(
                transaction, "SELECT * FROM sessions WHERE id = ?", (resolved_id,)
            )
        assert row is not None
        return self._to_record(row)

    @staticmethod
    def _to_record(row: aiosqlite.Row) -> SessionRecord:
        return SessionRecord(
            session_id=str(row["id"]),
            goal=str(row["goal"]),
            source_repo_path=Path(str(row["source_repo_path"])),
            shared_repo_path=Path(str(row["shared_repo_path"])),
            base_commit=_git_object_id(str(row["base_commit"])),
            integration_branch=str(row["integration_branch"]),
            integration_head_commit=_git_object_id(str(row["integration_head_commit"])),
            status=SessionStatus(str(row["status"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


class WorkflowRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(self, value: NewWorkflow, *, now: datetime | None = None) -> WorkflowRecord:
        workflow_id = _entity_id(value.workflow_id)
        session_id = _entity_id(value.session_id)
        parent_id = _entity_id(value.parent_workflow_id) if value.parent_workflow_id else None
        planner_run_id = (
            _entity_id(value.source_planner_run_id) if value.source_planner_run_id else None
        )
        author_json = canonical_json(value.author_graph).decode("utf-8")
        layout_json = canonical_json(value.layout).decode("utf-8")
        author_hash = _model_hash(value.author_graph)
        layout_hash = _model_hash(value.layout)
        timestamp = utc_now_text(now)

        async with self._database.immediate_transaction() as transaction:
            session = await _fetchone(
                transaction, "SELECT id FROM sessions WHERE id = ?", (session_id,)
            )
            if session is None:
                raise RecordNotFound(f"session not found: {session_id}")
            if parent_id is not None:
                parent = await _fetchone(
                    transaction, "SELECT session_id FROM workflows WHERE id = ?", (parent_id,)
                )
                if parent is None:
                    raise RecordNotFound(f"parent workflow not found: {parent_id}")
                if parent["session_id"] != session_id:
                    raise ValueError("parent workflow must belong to the same session")
            if planner_run_id is not None:
                planner = await _fetchone(
                    transaction,
                    "SELECT session_id FROM planner_runs WHERE id = ?",
                    (planner_run_id,),
                )
                if planner is None:
                    raise RecordNotFound(f"planner run not found: {planner_run_id}")
                if planner["session_id"] != session_id:
                    raise ValueError("source planner run must belong to the same session")

            await transaction.execute(
                """
                INSERT INTO workflows(
                    id, session_id, parent_workflow_id, source_planner_run_id,
                    semantic_version, layout_version, author_graph_json, author_graph_hash,
                    layout_json, layout_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow_id,
                    session_id,
                    parent_id,
                    planner_run_id,
                    author_json,
                    author_hash,
                    layout_json,
                    layout_hash,
                    timestamp,
                    timestamp,
                ),
            )
            row = await _fetchone(
                transaction, "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
            )
        assert row is not None
        return self._to_record(row)

    async def get(self, workflow_id: str) -> WorkflowRecord:
        resolved_id = _entity_id(workflow_id)
        async with self._database.connection() as connection:
            row = await _fetchone(
                Transaction(connection), "SELECT * FROM workflows WHERE id = ?", (resolved_id,)
            )
        if row is None:
            raise RecordNotFound(f"workflow not found: {resolved_id}")
        return self._to_record(row)

    async def update_author_graph(
        self,
        workflow_id: str,
        author_graph: AuthorGraph,
        *,
        expected_semantic_version: int,
        now: datetime | None = None,
    ) -> WorkflowRecord:
        if expected_semantic_version < 1:
            raise ValueError("expected_semantic_version must be positive")
        resolved_id = _entity_id(workflow_id)
        payload = canonical_json(author_graph).decode("utf-8")
        payload_hash = _model_hash(author_graph)
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            changed = await transaction.execute(
                """
                UPDATE workflows
                SET author_graph_json = ?, author_graph_hash = ?,
                    semantic_version = semantic_version + 1, updated_at = ?
                WHERE id = ? AND semantic_version = ?
                """,
                (payload, payload_hash, timestamp, resolved_id, expected_semantic_version),
            )
            if changed != 1:
                await self._raise_workflow_conflict(
                    transaction, resolved_id, "semantic_version", expected_semantic_version
                )
            row = await _fetchone(
                transaction, "SELECT * FROM workflows WHERE id = ?", (resolved_id,)
            )
        assert row is not None
        return self._to_record(row)

    async def update_layout(
        self,
        workflow_id: str,
        layout: WorkflowLayout,
        *,
        expected_layout_version: int,
        now: datetime | None = None,
    ) -> WorkflowRecord:
        if expected_layout_version < 1:
            raise ValueError("expected_layout_version must be positive")
        resolved_id = _entity_id(workflow_id)
        payload = canonical_json(layout).decode("utf-8")
        payload_hash = _model_hash(layout)
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            changed = await transaction.execute(
                """
                UPDATE workflows
                SET layout_json = ?, layout_hash = ?,
                    layout_version = layout_version + 1, updated_at = ?
                WHERE id = ? AND layout_version = ?
                """,
                (payload, payload_hash, timestamp, resolved_id, expected_layout_version),
            )
            if changed != 1:
                await self._raise_workflow_conflict(
                    transaction, resolved_id, "layout_version", expected_layout_version
                )
            row = await _fetchone(
                transaction, "SELECT * FROM workflows WHERE id = ?", (resolved_id,)
            )
        assert row is not None
        return self._to_record(row)

    @staticmethod
    async def _raise_workflow_conflict(
        transaction: Transaction,
        workflow_id: str,
        field: str,
        expected: int,
    ) -> None:
        row = await _fetchone(
            transaction, f"SELECT {field} FROM workflows WHERE id = ?", (workflow_id,)
        )
        if row is None:
            raise RecordNotFound(f"workflow not found: {workflow_id}")
        raise ConcurrencyConflict(
            f"workflow {workflow_id} expected {field}={expected}, found {row[field]}"
        )

    @staticmethod
    def _to_record(row: aiosqlite.Row) -> WorkflowRecord:
        return WorkflowRecord(
            workflow_id=str(row["id"]),
            session_id=str(row["session_id"]),
            parent_workflow_id=(
                str(row["parent_workflow_id"]) if row["parent_workflow_id"] is not None else None
            ),
            source_planner_run_id=(
                str(row["source_planner_run_id"])
                if row["source_planner_run_id"] is not None
                else None
            ),
            semantic_version=int(row["semantic_version"]),
            layout_version=int(row["layout_version"]),
            author_graph=AuthorGraph.model_validate_json(str(row["author_graph_json"])),
            author_graph_hash=_sha256(str(row["author_graph_hash"])),
            layout=WorkflowLayout.model_validate_json(str(row["layout_json"])),
            layout_hash=_sha256(str(row["layout_hash"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
