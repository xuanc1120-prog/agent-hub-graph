ALTER TABLE approvals
ADD COLUMN decision_request_sha256 TEXT
CHECK (decision_request_sha256 IS NULL OR length(decision_request_sha256) = 64);

INSERT INTO schema_migrations(version, applied_at)
VALUES (4, strftime('%Y-%m-%dT%H:%M:%f', 'now') || '000Z');
