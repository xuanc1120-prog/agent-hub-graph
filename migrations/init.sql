PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    applied_at TEXT NOT NULL CHECK (length(applied_at) = 27 AND substr(applied_at, -1) = 'Z')
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 500),
    adapter_type TEXT NOT NULL CHECK (length(adapter_type) BETWEEN 1 AND 64),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    executable_path TEXT,
    executable_sha256 TEXT CHECK (
        executable_sha256 IS NULL OR
        (length(executable_sha256) = 64 AND executable_sha256 NOT GLOB '*[^0-9a-f]*')
    ),
    detected_version TEXT CHECK (detected_version IS NULL OR length(detected_version) <= 200),
    capabilities_json TEXT NOT NULL CHECK (json_valid(capabilities_json)),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND substr(created_at, -1) = 'Z')
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    goal TEXT NOT NULL CHECK (length(goal) BETWEEN 1 AND 20000),
    source_repo_path TEXT NOT NULL CHECK (length(source_repo_path) BETWEEN 1 AND 4096),
    shared_repo_path TEXT NOT NULL UNIQUE CHECK (length(shared_repo_path) BETWEEN 1 AND 4096),
    base_commit TEXT NOT NULL CHECK (length(base_commit) IN (40, 64)),
    integration_branch TEXT NOT NULL CHECK (length(integration_branch) BETWEEN 1 AND 500),
    integration_head_commit TEXT NOT NULL CHECK (length(integration_head_commit) IN (40, 64)),
    status TEXT NOT NULL CHECK (status IN ('active', 'blocked', 'archived')),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND substr(created_at, -1) = 'Z'),
    updated_at TEXT NOT NULL CHECK (length(updated_at) = 27 AND substr(updated_at, -1) = 'Z')
);

CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    session_id TEXT NOT NULL REFERENCES sessions(id),
    parent_workflow_id TEXT REFERENCES workflows(id),
    source_planner_run_id TEXT REFERENCES planner_runs(id),
    semantic_version INTEGER NOT NULL CHECK (semantic_version >= 1),
    layout_version INTEGER NOT NULL CHECK (layout_version >= 1),
    author_graph_json TEXT NOT NULL CHECK (json_valid(author_graph_json)),
    author_graph_hash TEXT NOT NULL CHECK (length(author_graph_hash) = 64),
    layout_json TEXT NOT NULL CHECK (json_valid(layout_json)),
    layout_hash TEXT NOT NULL CHECK (length(layout_hash) = 64),
    last_compiled_graph_json TEXT CHECK (
        last_compiled_graph_json IS NULL OR json_valid(last_compiled_graph_json)
    ),
    last_compiled_graph_hash TEXT CHECK (
        last_compiled_graph_hash IS NULL OR length(last_compiled_graph_hash) = 64
    ),
    last_compiled_semantic_version INTEGER CHECK (
        last_compiled_semantic_version IS NULL OR last_compiled_semantic_version >= 1
    ),
    last_compiled_agent_catalog_hash TEXT CHECK (
        last_compiled_agent_catalog_hash IS NULL OR length(last_compiled_agent_catalog_hash) = 64
    ),
    last_compiled_base_commit TEXT CHECK (
        last_compiled_base_commit IS NULL OR length(last_compiled_base_commit) IN (40, 64)
    ),
    policy_version TEXT CHECK (policy_version IS NULL OR length(policy_version) BETWEEN 1 AND 64),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND substr(created_at, -1) = 'Z'),
    updated_at TEXT NOT NULL CHECK (length(updated_at) = 27 AND substr(updated_at, -1) = 'Z'),
    CHECK (parent_workflow_id IS NULL OR parent_workflow_id <> id)
);

CREATE TABLE IF NOT EXISTS planner_runs (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    session_id TEXT NOT NULL REFERENCES sessions(id),
    planner_id TEXT NOT NULL CHECK (length(planner_id) BETWEEN 1 AND 128),
    planner_type TEXT NOT NULL CHECK (planner_type IN ('rule_based', 'open_code')),
    planner_model TEXT CHECK (planner_model IS NULL OR length(planner_model) <= 200),
    integration_base_commit TEXT CHECK (
        integration_base_commit IS NULL OR length(integration_base_commit) IN (40, 64)
    ),
    context_bundle_artifact_id TEXT REFERENCES artifacts(id),
    context_bundle_sha256 TEXT CHECK (
        context_bundle_sha256 IS NULL OR length(context_bundle_sha256) = 64
    ),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'succeeded', 'failed', 'timed_out', 'cancelled', 'orphaned')
    ),
    runtime_policy_artifact_id TEXT REFERENCES artifacts(id),
    output_artifact_id TEXT REFERENCES artifacts(id),
    error_code TEXT CHECK (error_code IS NULL OR length(error_code) <= 200),
    fallback_from_run_id TEXT REFERENCES planner_runs(id),
    result_workflow_id TEXT REFERENCES workflows(id),
    result_semantic_version INTEGER CHECK (
        result_semantic_version IS NULL OR result_semantic_version >= 1
    ),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND substr(created_at, -1) = 'Z'),
    started_at TEXT CHECK (started_at IS NULL OR (length(started_at) = 27 AND substr(started_at, -1) = 'Z')),
    finished_at TEXT CHECK (finished_at IS NULL OR (length(finished_at) = 27 AND substr(finished_at, -1) = 'Z')),
    CHECK (fallback_from_run_id IS NULL OR fallback_from_run_id <> id)
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    session_id TEXT NOT NULL REFERENCES sessions(id),
    integration_base_commit TEXT NOT NULL CHECK (length(integration_base_commit) IN (40, 64)),
    current_commit TEXT NOT NULL CHECK (length(current_commit) IN (40, 64)),
    workflow_semantic_version INTEGER NOT NULL CHECK (workflow_semantic_version >= 1),
    workflow_layout_version INTEGER NOT NULL CHECK (workflow_layout_version >= 1),
    author_snapshot_json TEXT NOT NULL CHECK (json_valid(author_snapshot_json)),
    author_snapshot_hash TEXT NOT NULL CHECK (length(author_snapshot_hash) = 64),
    compiled_snapshot_json TEXT NOT NULL CHECK (json_valid(compiled_snapshot_json)),
    compiled_snapshot_hash TEXT NOT NULL CHECK (length(compiled_snapshot_hash) = 64),
    layout_snapshot_json TEXT NOT NULL CHECK (json_valid(layout_snapshot_json)),
    layout_snapshot_hash TEXT NOT NULL CHECK (length(layout_snapshot_hash) = 64),
    policy_version TEXT NOT NULL CHECK (length(policy_version) BETWEEN 1 AND 64),
    agent_catalog_snapshot_json TEXT NOT NULL CHECK (json_valid(agent_catalog_snapshot_json)),
    agent_catalog_snapshot_hash TEXT NOT NULL CHECK (length(agent_catalog_snapshot_hash) = 64),
    planner_run_id TEXT REFERENCES planner_runs(id),
    planner_id TEXT CHECK (planner_id IS NULL OR length(planner_id) BETWEEN 1 AND 128),
    planner_model TEXT CHECK (planner_model IS NULL OR length(planner_model) <= 200),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'waiting_approval', 'paused', 'blocked', 'failed', 'completed', 'cancelled', 'orphaned')
    ),
    next_event_seq INTEGER NOT NULL DEFAULT 1 CHECK (next_event_seq >= 1),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND substr(created_at, -1) = 'Z'),
    started_at TEXT CHECK (started_at IS NULL OR (length(started_at) = 27 AND substr(started_at, -1) = 'Z')),
    cancel_requested_at TEXT CHECK (
        cancel_requested_at IS NULL OR (length(cancel_requested_at) = 27 AND substr(cancel_requested_at, -1) = 'Z')
    ),
    merge_finalizing_at TEXT CHECK (
        merge_finalizing_at IS NULL OR (length(merge_finalizing_at) = 27 AND substr(merge_finalizing_at, -1) = 'Z')
    ),
    finished_at TEXT CHECK (finished_at IS NULL OR (length(finished_at) = 27 AND substr(finished_at, -1) = 'Z'))
);

CREATE TABLE IF NOT EXISTS node_runs (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
    node_id TEXT NOT NULL CHECK (length(node_id) BETWEEN 1 AND 128),
    node_type TEXT NOT NULL CHECK (
        node_type IN ('input', 'agent_task', 'context_builder', 'patch_guard', 'command_guard', 'test', 'risk_classifier', 'approval', 'merge_patch', 'output', 'if')
    ),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'ready', 'running', 'waiting_approval', 'blocked_by_guard', 'failed', 'completed', 'skipped', 'superseded', 'cancelled', 'orphaned')
    ),
    outcome TEXT CHECK (
        outcome IS NULL OR outcome IN ('success', 'failure', 'matched', 'not_matched', 'approved', 'rejected', 'blocked', 'cancelled')
    ),
    assigned_agent_id TEXT REFERENCES agents(id),
    input_hash TEXT CHECK (input_hash IS NULL OR length(input_hash) = 64),
    output_artifact_id TEXT REFERENCES artifacts(id),
    change_set_id TEXT REFERENCES change_sets(id),
    error_code TEXT CHECK (error_code IS NULL OR length(error_code) <= 200),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND substr(created_at, -1) = 'Z'),
    started_at TEXT CHECK (started_at IS NULL OR (length(started_at) = 27 AND substr(started_at, -1) = 'Z')),
    finished_at TEXT CHECK (finished_at IS NULL OR (length(finished_at) = 27 AND substr(finished_at, -1) = 'Z')),
    UNIQUE (workflow_run_id, node_id, attempt)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    node_run_id TEXT NOT NULL UNIQUE REFERENCES node_runs(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    base_commit TEXT NOT NULL CHECK (length(base_commit) IN (40, 64)),
    runtime_policy_artifact_id TEXT REFERENCES artifacts(id),
    active_capability_grant_id TEXT REFERENCES capability_grants(id),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'succeeded', 'privilege_requested', 'failed', 'timed_out', 'cancelled', 'blocked_by_guard', 'parse_failed', 'orphaned')
    ),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND substr(created_at, -1) = 'Z'),
    finished_at TEXT CHECK (finished_at IS NULL OR (length(finished_at) = 27 AND substr(finished_at, -1) = 'Z'))
);

CREATE TABLE IF NOT EXISTS task_permissions (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
    allowed_files_json TEXT NOT NULL CHECK (json_valid(allowed_files_json)),
    new_files_json TEXT NOT NULL CHECK (json_valid(new_files_json)),
    granted_existing_files_json TEXT NOT NULL CHECK (json_valid(granted_existing_files_json)),
    readonly_files_json TEXT NOT NULL CHECK (json_valid(readonly_files_json)),
    allowed_commands_json TEXT NOT NULL CHECK (json_valid(allowed_commands_json)),
    forbidden_paths_json TEXT NOT NULL CHECK (json_valid(forbidden_paths_json)),
    ephemeral_paths_json TEXT NOT NULL CHECK (json_valid(ephemeral_paths_json)),
    effective_risk TEXT NOT NULL CHECK (effective_risk IN ('L0', 'L1', 'L2', 'L3', 'L4')),
    permissions_hash TEXT NOT NULL CHECK (length(permissions_hash) = 64)
);

CREATE TABLE IF NOT EXISTS change_sets (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
    base_commit TEXT NOT NULL CHECK (length(base_commit) IN (40, 64)),
    pre_state_hash TEXT NOT NULL CHECK (length(pre_state_hash) = 64),
    post_state_hash TEXT NOT NULL CHECK (length(post_state_hash) = 64),
    patch_sha256 TEXT NOT NULL CHECK (length(patch_sha256) = 64),
    manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json)),
    patch_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    status TEXT NOT NULL CHECK (
        status IN ('captured', 'guard_passed', 'guard_rejected', 'test_passed', 'test_failed', 'policy_rejected', 'pending_approval', 'approved', 'rejected', 'cancelled', 'stale', 'merged', 'abandoned_partial', 'quarantined')
    ),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND substr(created_at, -1) = 'Z'),
    updated_at TEXT NOT NULL CHECK (length(updated_at) = 27 AND substr(updated_at, -1) = 'Z')
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    session_id TEXT NOT NULL REFERENCES sessions(id),
    task_id TEXT REFERENCES tasks(id),
    planner_run_id TEXT REFERENCES planner_runs(id),
    artifact_type TEXT NOT NULL CHECK (
        artifact_type IN ('log', 'console', 'diff', 'patch', 'report', 'test_result', 'runtime_policy', 'change_preimage')
    ),
    relative_path TEXT NOT NULL UNIQUE CHECK (length(relative_path) BETWEEN 1 AND 1024),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    redacted INTEGER NOT NULL CHECK (redacted IN (0, 1)),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND substr(created_at, -1) = 'Z'),
    CHECK (task_id IS NULL OR planner_run_id IS NULL)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    workflow_id TEXT REFERENCES workflows(id),
    workflow_run_id TEXT REFERENCES workflow_runs(id),
    run_seq INTEGER CHECK (run_seq IS NULL OR run_seq >= 1),
    event_type TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 128),
    actor_type TEXT NOT NULL CHECK (actor_type IN ('user', 'master', 'agent', 'system', 'local_cli')),
    actor_id TEXT CHECK (actor_id IS NULL OR length(actor_id) BETWEEN 1 AND 128),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND length(payload_json) <= 65536),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND substr(created_at, -1) = 'Z'),
    UNIQUE (workflow_run_id, run_seq),
    CHECK (
        (workflow_run_id IS NULL AND run_seq IS NULL) OR
        (workflow_run_id IS NOT NULL AND run_seq IS NOT NULL)
    ),
    CHECK (workflow_run_id IS NULL OR workflow_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    actor_scope TEXT NOT NULL CHECK (length(actor_scope) BETWEEN 1 AND 256),
    operation_scope TEXT NOT NULL CHECK (length(operation_scope) BETWEEN 1 AND 512),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 256),
    request_sha256 TEXT NOT NULL CHECK (
        length(request_sha256) = 64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    response_status INTEGER NOT NULL CHECK (response_status BETWEEN 100 AND 599),
    response_json TEXT NOT NULL CHECK (json_valid(response_json)),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND substr(created_at, -1) = 'Z'),
    expires_at TEXT NOT NULL CHECK (
        length(expires_at) = 27 AND substr(expires_at, -1) = 'Z' AND expires_at > created_at
    ),
    PRIMARY KEY (actor_scope, operation_scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS master_leases (
    lease_key TEXT PRIMARY KEY CHECK (length(lease_key) BETWEEN 1 AND 128),
    instance_id TEXT NOT NULL CHECK (length(instance_id) BETWEEN 1 AND 128),
    process_id INTEGER NOT NULL CHECK (process_id > 0),
    fencing_token INTEGER NOT NULL CHECK (fencing_token >= 1),
    heartbeat_at TEXT NOT NULL CHECK (length(heartbeat_at) = 27 AND substr(heartbeat_at, -1) = 'Z'),
    lease_expires_at TEXT NOT NULL CHECK (
        length(lease_expires_at) = 27 AND substr(lease_expires_at, -1) = 'Z'
    )
);

CREATE TABLE IF NOT EXISTS file_locks (
    resource_key TEXT PRIMARY KEY CHECK (length(resource_key) BETWEEN 1 AND 1024),
    owner_kind TEXT NOT NULL CHECK (length(owner_kind) BETWEEN 1 AND 64),
    owner_operation_id TEXT NOT NULL CHECK (length(owner_operation_id) BETWEEN 1 AND 128),
    owner_process_id INTEGER NOT NULL CHECK (owner_process_id > 0),
    fencing_token INTEGER NOT NULL CHECK (fencing_token >= 1),
    acquired_at TEXT NOT NULL CHECK (length(acquired_at) = 27 AND substr(acquired_at, -1) = 'Z'),
    heartbeat_at TEXT NOT NULL CHECK (length(heartbeat_at) = 27 AND substr(heartbeat_at, -1) = 'Z'),
    lease_expires_at TEXT NOT NULL CHECK (
        length(lease_expires_at) = 27 AND substr(lease_expires_at, -1) = 'Z'
    ),
    released_at TEXT CHECK (
        released_at IS NULL OR (length(released_at) = 27 AND substr(released_at, -1) = 'Z')
    )
);

CREATE TABLE IF NOT EXISTS console_sessions (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    session_id TEXT NOT NULL REFERENCES sessions(id),
    owner_type TEXT NOT NULL CHECK (owner_type IN ('task', 'planner_run')),
    owner_id TEXT NOT NULL CHECK (length(owner_id) BETWEEN 1 AND 128),
    workflow_run_id TEXT REFERENCES workflow_runs(id),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND substr(created_at, -1) = 'Z'),
    closed_at TEXT CHECK (closed_at IS NULL OR (length(closed_at) = 27 AND substr(closed_at, -1) = 'Z')),
    UNIQUE (owner_type, owner_id)
);

CREATE TABLE IF NOT EXISTS console_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    console_session_id TEXT NOT NULL REFERENCES console_sessions(id),
    seq INTEGER NOT NULL CHECK (seq >= 1),
    stream TEXT NOT NULL CHECK (stream IN ('stdout', 'stderr', 'system')),
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0 AND size_bytes <= 65536),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND substr(created_at, -1) = 'Z'),
    UNIQUE (console_session_id, seq)
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
    node_run_id TEXT NOT NULL REFERENCES node_runs(id),
    subject_type TEXT NOT NULL CHECK (subject_type IN ('change_set', 'privilege_request')),
    change_set_id TEXT REFERENCES change_sets(id),
    privilege_request_id TEXT REFERENCES privilege_requests(id),
    subject_sha256 TEXT NOT NULL CHECK (length(subject_sha256) = 64),
    base_commit TEXT CHECK (base_commit IS NULL OR length(base_commit) IN (40, 64)),
    patch_sha256 TEXT CHECK (patch_sha256 IS NULL OR length(patch_sha256) = 64),
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    effective_risk TEXT NOT NULL CHECK (effective_risk IN ('L0', 'L1', 'L2', 'L3', 'L4')),
    scope_json TEXT NOT NULL CHECK (json_valid(scope_json)),
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'invalidated')),
    version INTEGER NOT NULL CHECK (version >= 1),
    decision_actor TEXT CHECK (decision_actor IS NULL OR length(decision_actor) <= 128),
    decision_idempotency_key TEXT CHECK (
        decision_idempotency_key IS NULL OR length(decision_idempotency_key) <= 256
    ),
    expires_at TEXT NOT NULL CHECK (length(expires_at) = 27 AND substr(expires_at, -1) = 'Z'),
    decided_at TEXT CHECK (decided_at IS NULL OR (length(decided_at) = 27 AND substr(decided_at, -1) = 'Z')),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND substr(created_at, -1) = 'Z'),
    CHECK (
        (subject_type = 'change_set' AND change_set_id IS NOT NULL AND
         privilege_request_id IS NULL AND base_commit IS NOT NULL AND patch_sha256 IS NOT NULL) OR
        (subject_type = 'privilege_request' AND privilege_request_id IS NOT NULL AND
         change_set_id IS NULL AND base_commit IS NULL AND patch_sha256 IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS privilege_requests (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    task_id TEXT NOT NULL REFERENCES tasks(id),
    node_run_id TEXT NOT NULL REFERENCES node_runs(id),
    capability TEXT NOT NULL CHECK (capability IN ('modify_dependency', 'modify_config')),
    action TEXT NOT NULL CHECK (action IN ('edit_dependency_manifest', 'edit_project_config')),
    resource TEXT NOT NULL CHECK (length(resource) BETWEEN 1 AND 1024),
    effective_risk TEXT NOT NULL CHECK (effective_risk IN ('L0', 'L1', 'L2', 'L3', 'L4')),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'waiting_approval', 'approved', 'rejected', 'consumed', 'expired', 'denied')
    ),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND substr(created_at, -1) = 'Z')
);

CREATE TABLE IF NOT EXISTS capability_grants (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    request_id TEXT NOT NULL UNIQUE REFERENCES privilege_requests(id),
    target_task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
    action TEXT NOT NULL CHECK (action IN ('edit_dependency_manifest', 'edit_project_config')),
    resource TEXT NOT NULL CHECK (length(resource) BETWEEN 1 AND 1024),
    expires_at TEXT NOT NULL CHECK (length(expires_at) = 27 AND substr(expires_at, -1) = 'Z'),
    consumed_at TEXT CHECK (consumed_at IS NULL OR (length(consumed_at) = 27 AND substr(consumed_at, -1) = 'Z')),
    consumed_fencing_token INTEGER CHECK (
        consumed_fencing_token IS NULL OR consumed_fencing_token >= 1
    ),
    revoked_at TEXT CHECK (revoked_at IS NULL OR (length(revoked_at) = 27 AND substr(revoked_at, -1) = 'Z')),
    revocation_reason TEXT CHECK (revocation_reason IS NULL OR length(revocation_reason) <= 200),
    CHECK (
        (consumed_at IS NULL AND consumed_fencing_token IS NULL) OR
        (consumed_at IS NOT NULL AND consumed_fencing_token IS NOT NULL)
    ),
    CHECK (NOT (consumed_at IS NOT NULL AND revoked_at IS NOT NULL)),
    CHECK (
        (revoked_at IS NULL AND revocation_reason IS NULL) OR
        (revoked_at IS NOT NULL AND revocation_reason IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS security_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    workflow_run_id TEXT REFERENCES workflow_runs(id),
    task_id TEXT REFERENCES tasks(id),
    event_type TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 128),
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'high', 'critical')),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND length(payload_json) <= 65536),
    created_at TEXT NOT NULL CHECK (length(created_at) = 27 AND substr(created_at, -1) = 'Z')
);

CREATE INDEX IF NOT EXISTS idx_workflows_session ON workflows(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_planner_runs_session_status ON planner_runs(session_id, status);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow ON workflow_runs(workflow_id, created_at);
CREATE INDEX IF NOT EXISTS idx_node_runs_workflow_status ON node_runs(workflow_run_id, status);
CREATE INDEX IF NOT EXISTS idx_events_session_id ON events(session_id, id);
CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events(workflow_run_id, run_seq);
CREATE INDEX IF NOT EXISTS idx_console_messages_session_seq ON console_messages(console_session_id, seq);
CREATE INDEX IF NOT EXISTS idx_approvals_status_expiry ON approvals(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_idempotency_expiry ON idempotency_keys(expires_at);
CREATE INDEX IF NOT EXISTS idx_security_events_session_created ON security_events(session_id, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_runs_active_session
ON workflow_runs(session_id)
WHERE status IN ('pending', 'running', 'waiting_approval', 'paused', 'blocked', 'orphaned');

CREATE UNIQUE INDEX IF NOT EXISTS ux_approvals_pending_change_set
ON approvals(change_set_id)
WHERE subject_type = 'change_set' AND status = 'pending';

CREATE UNIQUE INDEX IF NOT EXISTS ux_approvals_pending_privilege_request
ON approvals(privilege_request_id)
WHERE subject_type = 'privilege_request' AND status = 'pending';

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%f', 'now') || '000Z');

COMMIT;
