BEGIN IMMEDIATE;

ALTER TABLE privilege_requests
ADD COLUMN resource_seal_json TEXT NOT NULL DEFAULT '{}'
CHECK (json_valid(resource_seal_json) AND length(resource_seal_json) <= 4096);

ALTER TABLE capability_grants
ADD COLUMN resource_seal_json TEXT NOT NULL DEFAULT '{}'
CHECK (json_valid(resource_seal_json) AND length(resource_seal_json) <= 4096);

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

INSERT INTO schema_migrations(version, applied_at)
VALUES (3, strftime('%Y-%m-%dT%H:%M:%f', 'now') || '000Z');

COMMIT;
