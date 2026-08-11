BEGIN IMMEDIATE;

ALTER TABLE privilege_requests
ADD COLUMN resource_seal_json TEXT NOT NULL DEFAULT '{}'
CHECK (json_valid(resource_seal_json) AND length(resource_seal_json) <= 4096);

ALTER TABLE capability_grants
ADD COLUMN resource_seal_json TEXT NOT NULL DEFAULT '{}'
CHECK (json_valid(resource_seal_json) AND length(resource_seal_json) <= 4096);

INSERT INTO schema_migrations(version, applied_at)
VALUES (3, strftime('%Y-%m-%dT%H:%M:%f', 'now') || '000Z');

COMMIT;
