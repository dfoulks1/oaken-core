-- Which operations an MCP client may call: the owner's setting, inside a ceiling fixed in code.
-- scope is ``op:<name>`` or ``plugin:<name>``. updated_by is ``config`` for rows seeded from the
-- config file.
CREATE TABLE operation_settings (
    scope       TEXT PRIMARY KEY,
    mcp_enabled INTEGER NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL
);

-- The operations an agent token may call beyond the job protocol: operation names and
-- ``plugin:<name>`` entries. Empty: it can work its jobs and call nothing else.
ALTER TABLE agents ADD COLUMN operations_json TEXT NOT NULL DEFAULT '[]';
