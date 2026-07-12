from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import aiosqlite
import pytest

from protocol import (
    ActorType,
    ApprovalStatus,
    ArtifactType,
    CapabilityType,
    ChangeSetStatus,
    ConsoleOwnerType,
    ConsoleStreamKind,
    NodeOutcome,
    NodeRunStatus,
    NodeType,
    PlannerRunStatus,
    PrivilegeAction,
    PrivilegeRequestStatus,
    RiskLevel,
    SecuritySeverity,
    SessionStatus,
    TaskStatus,
    WorkflowRunStatus,
)
from storage.db import SCHEMA_VERSION, Database, utc_now_text
from storage.errors import UnsupportedSchemaVersion

EXPECTED_TABLES = {
    "agents",
    "approvals",
    "artifacts",
    "capability_grants",
    "change_sets",
    "console_messages",
    "console_sessions",
    "events",
    "file_locks",
    "idempotency_keys",
    "master_leases",
    "node_runs",
    "planner_runs",
    "privilege_requests",
    "schema_migrations",
    "security_events",
    "sessions",
    "task_permissions",
    "tasks",
    "workflow_runs",
    "workflows",
}
TIMESTAMP = "2026-07-12T00:00:00.000000Z"


async def test_initialize_is_idempotent_and_creates_v1_schema(database: Database) -> None:
    assert await database.initialize() == SCHEMA_VERSION
    async with database.connection() as connection:
        cursor = await connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        tables = {str(row[0]) for row in await cursor.fetchall()}
        await cursor.close()
        cursor = await connection.execute(
            "SELECT version, length(applied_at) FROM schema_migrations"
        )
        migration = await cursor.fetchone()
        await cursor.close()
        cursor = await connection.execute("PRAGMA foreign_key_check")
        foreign_key_errors = await cursor.fetchall()
        await cursor.close()
        cursor = await connection.execute("PRAGMA integrity_check")
        integrity = await cursor.fetchone()
        await cursor.close()

    assert tables == EXPECTED_TABLES
    assert tuple(migration) == (1, 27)
    assert foreign_key_errors == []
    assert integrity is not None and integrity[0] == "ok"


async def test_every_connection_has_required_pragmas(database: Database) -> None:
    async with database.connection() as connection:
        foreign_keys = await _pragma(connection, "foreign_keys")
        journal_mode = await _pragma(connection, "journal_mode")
        busy_timeout = await _pragma(connection, "busy_timeout")

    assert foreign_keys == 1
    assert str(journal_mode).lower() == "wal"
    assert busy_timeout == 5_000


async def test_schema_enforces_foreign_keys_and_status_checks(database: Database) -> None:
    async with database.connection() as connection:
        with pytest.raises(aiosqlite.IntegrityError):
            await connection.execute(
                """
                INSERT INTO workflows(
                    id, session_id, semantic_version, layout_version,
                    author_graph_json, author_graph_hash, layout_json, layout_hash,
                    created_at, updated_at
                ) VALUES ('wf1', 'missing', 1, 1, '{}', ?, '{}', ?, ?, ?)
                """,
                ("a" * 64, "b" * 64, TIMESTAMP, TIMESTAMP),
            )

        with pytest.raises(aiosqlite.IntegrityError):
            await connection.execute(
                """
                INSERT INTO sessions(
                    id, goal, source_repo_path, shared_repo_path, base_commit,
                    integration_branch, integration_head_commit, status, created_at, updated_at
                ) VALUES ('s1', 'goal', 'source', 'shared', ?, 'main', ?, 'invalid', ?, ?)
                """,
                ("a" * 40, "a" * 40, TIMESTAMP, TIMESTAMP),
            )


async def test_required_and_partial_indexes_exist(database: Database) -> None:
    expected = {
        "idx_node_runs_workflow_status",
        "idx_events_session_id",
        "idx_events_run_seq",
        "idx_console_messages_session_seq",
        "idx_approvals_status_expiry",
        "idx_idempotency_expiry",
        "idx_security_events_session_created",
        "ux_workflow_runs_active_session",
        "ux_approvals_pending_change_set",
        "ux_approvals_pending_privilege_request",
    }
    async with database.connection() as connection:
        cursor = await connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        names = {str(row[0]) for row in await cursor.fetchall()}
        await cursor.close()
    assert expected <= names


async def test_active_workflow_run_partial_unique_index_is_enforced(database: Database) -> None:
    run_sql = """
        INSERT INTO workflow_runs(
            id, workflow_id, session_id, integration_base_commit, current_commit,
            workflow_semantic_version, workflow_layout_version,
            author_snapshot_json, author_snapshot_hash,
            compiled_snapshot_json, compiled_snapshot_hash,
            layout_snapshot_json, layout_snapshot_hash, policy_version,
            agent_catalog_snapshot_json, agent_catalog_snapshot_hash,
            status, created_at
        ) VALUES (?, 'wf1', 's1', ?, ?, 1, 1, '{}', ?, '{}', ?, '{}', ?, '1', '{}', ?, ?, ?)
    """
    hashes = ("a" * 40, "a" * 40, "a" * 64, "b" * 64, "c" * 64, "d" * 64)
    async with database.immediate_transaction() as transaction:
        await transaction.execute(
            """
            INSERT INTO sessions(
                id, goal, source_repo_path, shared_repo_path, base_commit,
                integration_branch, integration_head_commit, status, created_at, updated_at
            ) VALUES ('s1', 'goal', 'source', 'shared', ?, 'main', ?, 'active', ?, ?)
            """,
            ("a" * 40, "a" * 40, TIMESTAMP, TIMESTAMP),
        )
        await transaction.execute(
            """
            INSERT INTO workflows(
                id, session_id, semantic_version, layout_version,
                author_graph_json, author_graph_hash, layout_json, layout_hash,
                created_at, updated_at
            ) VALUES ('wf1', 's1', 1, 1, '{}', ?, '{}', ?, ?, ?)
            """,
            ("a" * 64, "b" * 64, TIMESTAMP, TIMESTAMP),
        )
        await transaction.execute(run_sql, ("run-1", *hashes, "pending", TIMESTAMP))
        with pytest.raises(aiosqlite.IntegrityError):
            await transaction.execute(run_sql, ("run-2", *hashes, "running", TIMESTAMP))
        await transaction.execute(run_sql, ("run-2", *hashes, "completed", TIMESTAMP))


async def test_enum_checks_cover_every_frozen_protocol_value(database: Database) -> None:
    expected = {
        "sessions": (SessionStatus,),
        "planner_runs": (PlannerRunStatus,),
        "workflow_runs": (WorkflowRunStatus,),
        "node_runs": (NodeRunStatus, NodeType, NodeOutcome),
        "tasks": (TaskStatus,),
        "task_permissions": (RiskLevel,),
        "change_sets": (ChangeSetStatus,),
        "artifacts": (ArtifactType,),
        "events": (ActorType,),
        "console_sessions": (ConsoleOwnerType,),
        "console_messages": (ConsoleStreamKind,),
        "approvals": (ApprovalStatus, RiskLevel),
        "privilege_requests": (
            PrivilegeRequestStatus,
            CapabilityType,
            PrivilegeAction,
            RiskLevel,
        ),
        "capability_grants": (PrivilegeAction,),
        "security_events": (SecuritySeverity,),
    }
    async with database.connection() as connection:
        for table, enum_types in expected.items():
            cursor = await connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            )
            row = await cursor.fetchone()
            await cursor.close()
            assert row is not None
            sql = str(row[0])
            for enum_type in enum_types:
                for member in enum_type:
                    assert f"'{member.value}'" in sql, f"{table} does not constrain {member.value}"


async def test_newer_schema_fails_before_applying_v1(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        f"""
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_migrations VALUES (2, '{TIMESTAMP}');
        """
    )
    connection.close()

    with pytest.raises(UnsupportedSchemaVersion):
        await Database(path).initialize()

    connection = sqlite3.connect(path)
    agents = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agents'"
    ).fetchone()
    connection.close()
    assert agents is None


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_now_text(datetime(2026, 7, 12))


async def _pragma(connection: aiosqlite.Connection, name: str) -> object:
    cursor = await connection.execute(f"PRAGMA {name}")
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None
    return row[0]
