BEGIN IMMEDIATE;

ALTER TABLE privilege_requests
ADD COLUMN resource_seal_json TEXT NOT NULL DEFAULT '{}'
CHECK (json_valid(resource_seal_json) AND length(resource_seal_json) <= 4096);

ALTER TABLE capability_grants
ADD COLUMN resource_seal_json TEXT NOT NULL DEFAULT '{}'
CHECK (json_valid(resource_seal_json) AND length(resource_seal_json) <= 4096);

-- Capture valid lineage before changing state so the fail-closed migration is
-- explainable through the same typed workflow events as runtime transitions.
CREATE TEMP TABLE _hub210_v3_migration_meta AS
SELECT strftime('%Y-%m-%dT%H:%M:%f', 'now') || '000Z' AS created_at;

CREATE TEMP TABLE _hub210_v3_legacy_requests AS
SELECT pr.id AS request_id,
       pr.task_id,
       pr.node_run_id,
       nr.workflow_run_id,
       pr.status AS previous_status
FROM privilege_requests AS pr
JOIN node_runs AS nr
  ON nr.id = pr.node_run_id
JOIN workflow_runs AS wr
  ON wr.id = nr.workflow_run_id
WHERE pr.resource_seal_json = '{}'
  AND pr.status IN ('pending', 'waiting_approval', 'approved');

CREATE TEMP TABLE _hub210_v3_legacy_approvals AS
SELECT ap.id AS approval_id,
       ap.workflow_run_id,
       ap.node_run_id,
       ap.subject_type,
       ap.subject_sha256,
       ap.version AS previous_version
FROM approvals AS ap
JOIN privilege_requests AS pr
  ON pr.id = ap.privilege_request_id
JOIN node_runs AS nr
  ON nr.id = ap.node_run_id
 AND nr.workflow_run_id = ap.workflow_run_id
JOIN workflow_runs AS wr
  ON wr.id = ap.workflow_run_id
WHERE ap.status = 'pending'
  AND pr.resource_seal_json = '{}';

CREATE TEMP TABLE _hub210_v3_legacy_grants AS
SELECT cg.id AS grant_id,
       cg.request_id,
       cg.target_task_id,
       cg.action,
       cg.resource,
       pr.node_run_id,
       nr.workflow_run_id
FROM capability_grants AS cg
JOIN privilege_requests AS pr
  ON pr.id = cg.request_id
JOIN node_runs AS nr
  ON nr.id = pr.node_run_id
JOIN workflow_runs AS wr
  ON wr.id = nr.workflow_run_id
WHERE cg.resource_seal_json = '{}'
  AND cg.consumed_at IS NULL
  AND cg.revoked_at IS NULL;

-- Existing v2 rows have no trustworthy handle/content seal.  Invalidate every
-- still-actionable request, approval, and grant instead of allowing a legacy
-- record to cross the new capability boundary.  Consumed grants remain
-- historical facts and cannot be both consumed and revoked under the schema.
UPDATE approvals
SET status = 'rejected',
    version = version + 1,
    decision_actor = 'schema-migration-v3',
    decision_idempotency_key = 'schema-migration-v3',
    decided_at = strftime('%Y-%m-%dT%H:%M:%f', 'now') || '000Z'
WHERE status = 'pending'
  AND privilege_request_id IN (
      SELECT id FROM privilege_requests WHERE resource_seal_json = '{}'
  );

UPDATE privilege_requests
SET status = 'denied'
WHERE resource_seal_json = '{}'
  AND status IN ('pending', 'waiting_approval', 'approved');

UPDATE capability_grants
SET revoked_at = strftime('%Y-%m-%dT%H:%M:%f', 'now') || '000Z',
    revocation_reason = 'resource seal missing during schema migration'
WHERE resource_seal_json = '{}'
  AND consumed_at IS NULL
  AND revoked_at IS NULL;

-- The migration actor is intentionally explicit and deterministic.  The
-- event payloads use the v3 reason and preserve the pre-transition status or
-- version captured above.  All rows share one migration timestamp.
INSERT INTO events(
    session_id, workflow_id, workflow_run_id, run_seq, event_type,
    actor_type, actor_id, payload_json, created_at
)
SELECT wr.session_id,
       wr.workflow_id,
       candidate.workflow_run_id,
       wr.next_event_seq + candidate.offset,
       'workflow.privilege_request_state_changed',
       'system',
       'schema-migration-v3',
       json_object(
           'master_fencing_token', 1,
           'workflow_run_id', candidate.workflow_run_id,
           'node_run_id', candidate.node_run_id,
           'task_id', candidate.task_id,
           'request_id', candidate.request_id,
           'previous_status', candidate.previous_status,
           'status', 'denied',
           'reason', 'schema migration v3: capability resource seal missing'
       ),
       (SELECT created_at FROM _hub210_v3_migration_meta)
FROM (
    SELECT request_id,
           task_id,
           node_run_id,
           workflow_run_id,
           previous_status,
           ROW_NUMBER() OVER (
               PARTITION BY workflow_run_id ORDER BY request_id
           ) - 1 AS offset
    FROM _hub210_v3_legacy_requests
) AS candidate
JOIN workflow_runs AS wr
  ON wr.id = candidate.workflow_run_id;

UPDATE workflow_runs
SET next_event_seq = next_event_seq + (
    SELECT COUNT(*)
    FROM _hub210_v3_legacy_requests AS candidate
    WHERE candidate.workflow_run_id = workflow_runs.id
)
WHERE id IN (SELECT workflow_run_id FROM _hub210_v3_legacy_requests);

INSERT INTO events(
    session_id, workflow_id, workflow_run_id, run_seq, event_type,
    actor_type, actor_id, payload_json, created_at
)
SELECT wr.session_id,
       wr.workflow_id,
       candidate.workflow_run_id,
       wr.next_event_seq + candidate.offset,
       'workflow.approval_state_changed',
       'system',
       'schema-migration-v3',
       json_object(
           'master_fencing_token', 1,
           'workflow_run_id', candidate.workflow_run_id,
           'node_run_id', candidate.node_run_id,
           'approval_id', candidate.approval_id,
           'subject_type', candidate.subject_type,
           'previous_status', 'pending',
           'status', 'rejected',
           'version', candidate.previous_version + 1,
           'subject_sha256', candidate.subject_sha256,
           'reason', 'schema migration v3: capability resource seal missing'
       ),
       (SELECT created_at FROM _hub210_v3_migration_meta)
FROM (
    SELECT approval_id,
           workflow_run_id,
           node_run_id,
           subject_type,
           subject_sha256,
           previous_version,
           ROW_NUMBER() OVER (
               PARTITION BY workflow_run_id ORDER BY approval_id
           ) - 1 AS offset
    FROM _hub210_v3_legacy_approvals
) AS candidate
JOIN workflow_runs AS wr
  ON wr.id = candidate.workflow_run_id;

UPDATE workflow_runs
SET next_event_seq = next_event_seq + (
    SELECT COUNT(*)
    FROM _hub210_v3_legacy_approvals AS candidate
    WHERE candidate.workflow_run_id = workflow_runs.id
)
WHERE id IN (SELECT workflow_run_id FROM _hub210_v3_legacy_approvals);

INSERT INTO events(
    session_id, workflow_id, workflow_run_id, run_seq, event_type,
    actor_type, actor_id, payload_json, created_at
)
SELECT wr.session_id,
       wr.workflow_id,
       candidate.workflow_run_id,
       wr.next_event_seq + candidate.offset,
       'workflow.capability_grant_state_changed',
       'system',
       'schema-migration-v3',
       json_object(
           'master_fencing_token', 1,
           'workflow_run_id', candidate.workflow_run_id,
           'node_run_id', candidate.node_run_id,
           'request_id', candidate.request_id,
           'grant_id', candidate.grant_id,
           'target_task_id', candidate.target_task_id,
           'action', candidate.action,
           'resource', candidate.resource,
           'consumed_fencing_token', NULL,
           'reason', 'schema migration v3: resource seal missing; grant revoked'
       ),
       (SELECT created_at FROM _hub210_v3_migration_meta)
FROM (
    SELECT grant_id,
           request_id,
           target_task_id,
           action,
           resource,
           node_run_id,
           workflow_run_id,
           ROW_NUMBER() OVER (
               PARTITION BY workflow_run_id ORDER BY grant_id
           ) - 1 AS offset
    FROM _hub210_v3_legacy_grants
) AS candidate
JOIN workflow_runs AS wr
  ON wr.id = candidate.workflow_run_id;

UPDATE workflow_runs
SET next_event_seq = next_event_seq + (
    SELECT COUNT(*)
    FROM _hub210_v3_legacy_grants AS candidate
    WHERE candidate.workflow_run_id = workflow_runs.id
)
WHERE id IN (SELECT workflow_run_id FROM _hub210_v3_legacy_grants);

DROP TABLE _hub210_v3_legacy_requests;
DROP TABLE _hub210_v3_legacy_approvals;
DROP TABLE _hub210_v3_legacy_grants;
DROP TABLE _hub210_v3_migration_meta;

INSERT INTO schema_migrations(version, applied_at)
VALUES (3, strftime('%Y-%m-%dT%H:%M:%f', 'now') || '000Z');

COMMIT;
