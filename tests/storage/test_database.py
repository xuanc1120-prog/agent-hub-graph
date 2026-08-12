from __future__ import annotations

import asyncio
import json
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
        migrations = await cursor.fetchall()
        await cursor.close()
        cursor = await connection.execute("PRAGMA foreign_key_check")
        foreign_key_errors = await cursor.fetchall()
        await cursor.close()
        cursor = await connection.execute("PRAGMA integrity_check")
        integrity = await cursor.fetchone()
        await cursor.close()

    assert tables == EXPECTED_TABLES
    assert [tuple(row) for row in migrations] == [
        (version, 27) for version in range(1, SCHEMA_VERSION + 1)
    ]
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


@pytest.mark.parametrize("seed_v1", [False, True], ids=["fresh", "v1"])
async def test_concurrent_initializers_share_one_migration_lock(
    tmp_path: Path,
    *,
    seed_v1: bool,
) -> None:
    path = tmp_path / f"concurrent-{seed_v1}.db"
    if seed_v1:
        migration = (Path(__file__).resolve().parents[2] / "migrations" / "init.sql").read_text(
            encoding="utf-8"
        )
        connection = sqlite3.connect(path)
        connection.executescript(migration)
        connection.close()

    release = asyncio.Event()
    all_ready = asyncio.Event()
    ready = 0

    async def initialize(database: Database) -> int:
        nonlocal ready
        ready += 1
        if ready == 2:
            all_ready.set()
        await release.wait()
        return await database.initialize()

    tasks = [
        asyncio.create_task(initialize(Database(path))),
        asyncio.create_task(initialize(Database(path))),
    ]
    await all_ready.wait()
    release.set()

    assert await asyncio.gather(*tasks) == [SCHEMA_VERSION, SCHEMA_VERSION]
    connection = sqlite3.connect(path)
    versions = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(agents)").fetchall()}
    connection.close()
    assert versions == [(version,) for version in range(1, SCHEMA_VERSION + 1)]
    assert {"available", "auto_assignable", "unavailable_reason"} <= columns


async def test_newer_schema_fails_before_applying_v1(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        f"""
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_migrations VALUES ({SCHEMA_VERSION + 1}, '{TIMESTAMP}');
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


async def test_v2_capability_rows_are_invalidated_during_v3_migration(tmp_path: Path) -> None:
    path = tmp_path / "v2-capability-records.db"
    migration = (Path(__file__).resolve().parents[2] / "migrations" / "init.sql").read_text(
        encoding="utf-8"
    )
    connection = sqlite3.connect(path)
    connection.executescript(migration)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        """
        INSERT INTO privilege_requests(
            id, task_id, node_run_id, capability, action, resource,
            effective_risk, status, created_at
        ) VALUES ('priv-v2', 'task-v2', 'node-v2', 'modify_config',
                  'edit_project_config', 'config/settings.json', 'L1', 'approved', ?)
        """,
        (TIMESTAMP,),
    )
    connection.execute(
        """
        INSERT INTO approvals(
            id, workflow_run_id, node_run_id, subject_type, change_set_id,
            privilege_request_id, subject_sha256, base_commit, patch_sha256,
            evidence_sha256, effective_risk, scope_json, status, version,
            decision_actor, decision_idempotency_key, expires_at, decided_at, created_at
        ) VALUES ('approval-v2', 'run-v2', 'node-v2', 'privilege_request', NULL,
                  'priv-v2', ?, NULL, NULL, ?, 'L1', '["config/settings.json"]',
                  'pending', 1, NULL, NULL, ?, NULL, ?)
        """,
        ("a" * 64, "b" * 64, TIMESTAMP, TIMESTAMP),
    )
    connection.execute(
        """
        INSERT INTO capability_grants(
            id, request_id, target_task_id, action, resource, expires_at
        ) VALUES ('grant-v2', 'priv-v2', 'task-v2-retry',
                  'edit_project_config', 'config/settings.json', ?)
        """,
        (TIMESTAMP,),
    )
    connection.commit()
    connection.close()

    assert await Database(path).initialize() == SCHEMA_VERSION

    connection = sqlite3.connect(path)
    approval = connection.execute(
        "SELECT status FROM approvals WHERE id = 'approval-v2'"
    ).fetchone()
    request = connection.execute(
        "SELECT status FROM privilege_requests WHERE id = 'priv-v2'"
    ).fetchone()
    grant = connection.execute(
        "SELECT revoked_at, revocation_reason, resource_seal_json "
        "FROM capability_grants WHERE id = 'grant-v2'"
    ).fetchone()
    connection.close()

    assert approval == ("rejected",)
    assert request == ("denied",)
    assert grant is not None
    assert grant[0] is not None
    assert grant[1] == "resource seal missing during schema migration"
    assert grant[2] == "{}"


async def test_v3_migration_records_typed_events_for_valid_capability_lineage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v2-capability-lineage.db"
    migration = (Path(__file__).resolve().parents[2] / "migrations" / "init.sql").read_text(
        encoding="utf-8"
    )
    connection = sqlite3.connect(path)
    connection.executescript(migration)
    connection.executescript(
        f"""
        INSERT INTO agents(
            id, display_name, adapter_type, enabled, capabilities_json, created_at
        ) VALUES ('agent-v2', 'Agent v2', 'test', 1, '{{}}', '{TIMESTAMP}');

        INSERT INTO master_leases(
            lease_key, instance_id, process_id, fencing_token,
            heartbeat_at, lease_expires_at
        ) VALUES (
            'scheduler', 'historical-master-v2', 1, 7,
            '{TIMESTAMP}', '2026-07-13T00:00:00.000000Z'
        );

        INSERT INTO sessions(
            id, goal, source_repo_path, shared_repo_path, base_commit,
            integration_branch, integration_head_commit, status, created_at, updated_at
        ) VALUES (
            'session-v2', 'migration replay', 'source', 'shared-v2',
            '{"a" * 40}', 'main', '{"b" * 40}', 'active',
            '{TIMESTAMP}', '{TIMESTAMP}'
        );

        INSERT INTO workflows(
            id, session_id, semantic_version, layout_version,
            author_graph_json, author_graph_hash, layout_json, layout_hash,
            created_at, updated_at
        ) VALUES (
            'workflow-v2', 'session-v2', 1, 1, '{{}}', '{"c" * 64}',
            '{{}}', '{"d" * 64}', '{TIMESTAMP}', '{TIMESTAMP}'
        );

        INSERT INTO workflow_runs(
            id, workflow_id, session_id, integration_base_commit, current_commit,
            workflow_semantic_version, workflow_layout_version,
            author_snapshot_json, author_snapshot_hash,
            compiled_snapshot_json, compiled_snapshot_hash,
            layout_snapshot_json, layout_snapshot_hash, policy_version,
            agent_catalog_snapshot_json, agent_catalog_snapshot_hash,
            status, next_event_seq, created_at
        ) VALUES (
            'run-v2', 'workflow-v2', 'session-v2', '{"a" * 40}', '{"a" * 40}',
            1, 1, '{{}}', '{"e" * 64}', '{{}}', '{"f" * 64}',
            '{{}}', '{"0" * 64}', '1', '{{}}', '{"1" * 64}',
            'waiting_approval', 1, '{TIMESTAMP}'
        );

        INSERT INTO node_runs(
            id, workflow_run_id, node_id, node_type, attempt, status,
            assigned_agent_id, created_at
        ) VALUES (
            'node-v2', 'run-v2', 'agent-task', 'agent_task', 1, 'waiting_approval',
            'agent-v2', '{TIMESTAMP}'
        );

        INSERT INTO node_runs(
            id, workflow_run_id, node_id, node_type, attempt, status,
            assigned_agent_id, created_at
        ) VALUES (
            'node-v2-retry', 'run-v2', 'agent-task', 'agent_task', 2, 'ready',
            'agent-v2', '{TIMESTAMP}'
        );

        INSERT INTO tasks(
            id, node_run_id, agent_id, base_commit, status, created_at
        ) VALUES (
            'task-v2', 'node-v2', 'agent-v2', '{"a" * 40}',
            'privilege_requested', '{TIMESTAMP}'
        );

        INSERT INTO tasks(
            id, node_run_id, agent_id, base_commit, status, created_at
        ) VALUES (
            'task-v2-retry', 'node-v2-retry', 'agent-v2', '{"a" * 40}',
            'pending', '{TIMESTAMP}'
        );

        INSERT INTO privilege_requests(
            id, task_id, node_run_id, capability, action, resource,
            effective_risk, status, created_at
        ) VALUES (
            'priv-v2-valid', 'task-v2', 'node-v2', 'modify_config',
            'edit_project_config', 'config/settings.json', 'L1', 'pending',
            '{TIMESTAMP}'
        );

        INSERT INTO approvals(
            id, workflow_run_id, node_run_id, subject_type, change_set_id,
            privilege_request_id, subject_sha256, base_commit, patch_sha256,
            evidence_sha256, effective_risk, scope_json, status, version,
            decision_actor, decision_idempotency_key, expires_at, decided_at,
            created_at
        ) VALUES (
            'approval-v2-valid', 'run-v2', 'node-v2', 'privilege_request', NULL,
            'priv-v2-valid', '{"a" * 64}', NULL, NULL, '{"b" * 64}', 'L1',
            '["config/settings.json"]', 'pending', 3, NULL, NULL,
            '2026-07-13T00:00:00.000000Z', NULL, '{TIMESTAMP}'
        );

        INSERT INTO capability_grants(
            id, request_id, target_task_id, action, resource, expires_at
        ) VALUES (
            'grant-v2-valid', 'priv-v2-valid', 'task-v2-retry',
            'edit_project_config', 'config/settings.json',
            '2026-07-13T00:00:00.000000Z'
        );
        """
    )
    connection.commit()
    connection.close()

    assert await Database(path).initialize() == SCHEMA_VERSION

    connection = sqlite3.connect(path)
    events = connection.execute(
        """
        SELECT event_type, actor_type, actor_id, run_seq, payload_json
        FROM events
        WHERE workflow_run_id = 'run-v2'
        ORDER BY run_seq
        """
    ).fetchall()
    next_event_seq = connection.execute(
        "SELECT next_event_seq FROM workflow_runs WHERE id = 'run-v2'"
    ).fetchone()
    states = connection.execute(
        """
        SELECT
            (SELECT status FROM privilege_requests WHERE id = 'priv-v2-valid'),
            (SELECT status FROM approvals WHERE id = 'approval-v2-valid'),
            (SELECT version FROM approvals WHERE id = 'approval-v2-valid'),
            (SELECT revoked_at IS NOT NULL FROM capability_grants WHERE id = 'grant-v2-valid')
        """
    ).fetchone()
    connection.close()

    assert [row[:4] for row in events] == [
        (
            "workflow.schema_migration_state_changed",
            "system",
            "schema-migration-v3",
            1,
        ),
        (
            "workflow.schema_migration_state_changed",
            "system",
            "schema-migration-v3",
            2,
        ),
        (
            "workflow.schema_migration_state_changed",
            "system",
            "schema-migration-v3",
            3,
        ),
    ]
    from workflow.events import build_runtime_event_registry

    registry = build_runtime_event_registry()
    for event_type, *_metadata, payload_json in events:
        registry.validate_payload_json(event_type, payload_json)

    request_payload, approval_payload, grant_payload = [json.loads(row[4]) for row in events]
    assert all("master_fencing_token" not in json.loads(row[4]) for row in events)
    assert [
        payload["provenance"] for payload in (request_payload, approval_payload, grant_payload)
    ] == [
        "schema_migration",
        "schema_migration",
        "schema_migration",
    ]
    assert [
        payload["migration_version"]
        for payload in (request_payload, approval_payload, grant_payload)
    ] == [
        3,
        3,
        3,
    ]
    assert request_payload["subject_type"] == "privilege_request"
    assert request_payload["subject_id"] == "priv-v2-valid"
    assert request_payload["previous_status"] == "pending"
    assert request_payload["status"] == "denied"
    assert approval_payload["subject_type"] == "approval"
    assert approval_payload["subject_id"] == "approval-v2-valid"
    assert approval_payload["previous_status"] == "pending"
    assert approval_payload["status"] == "rejected"
    assert approval_payload["version"] == 4
    assert approval_payload["subject_sha256"] == "a" * 64
    assert grant_payload["subject_type"] == "capability_grant"
    assert grant_payload["subject_id"] == "grant-v2-valid"
    assert grant_payload["previous_status"] == "active"
    assert grant_payload["status"] == "revoked"
    assert grant_payload["reason"] == ("schema migration v3: resource seal missing; grant revoked")
    assert next_event_seq == (4,)
    assert states == ("denied", "rejected", 4, 1)


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_now_text(datetime(2026, 7, 12))


async def _pragma(connection: aiosqlite.Connection, name: str) -> object:
    cursor = await connection.execute(f"PRAGMA {name}")
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None
    return row[0]
