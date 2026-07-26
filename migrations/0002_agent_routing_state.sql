BEGIN IMMEDIATE;

ALTER TABLE agents
ADD COLUMN available INTEGER NOT NULL DEFAULT 0
CHECK (available IN (0, 1));

ALTER TABLE agents
ADD COLUMN auto_assignable INTEGER NOT NULL DEFAULT 0
CHECK (auto_assignable IN (0, 1));

ALTER TABLE agents
ADD COLUMN unavailable_reason TEXT
CHECK (unavailable_reason IS NULL OR length(unavailable_reason) <= 500);

UPDATE agents
SET available = CASE
        WHEN adapter_type = 'mock' AND enabled = 1 THEN 1
        ELSE 0
    END,
    auto_assignable = 0,
    unavailable_reason = CASE
        WHEN adapter_type = 'mock' AND enabled = 1 THEN NULL
        ELSE 'availability requires a fresh capability probe after schema migration'
    END;

INSERT INTO schema_migrations(version, applied_at)
VALUES (2, strftime('%Y-%m-%dT%H:%M:%f', 'now') || '000Z');

COMMIT;
