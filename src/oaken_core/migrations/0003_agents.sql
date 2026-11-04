-- External agents: separately authenticated workers that lease jobs.
-- token_hash is the SHA-256 of the agent token; the token itself is never stored.
-- kinds_json lists the job kinds the agent may lease; queues_json is NULL for every queue.
CREATE TABLE agents (
    id           TEXT PRIMARY KEY,
    label        TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    kinds_json   TEXT NOT NULL,
    queues_json  TEXT,
    max_leases   INTEGER NOT NULL,
    max_per_hour INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT,
    revoked_at   TEXT
);

-- The last agent to lease a job and when (the per-hour quota counts these), and the model the
-- finisher says produced the result (self-reported).
ALTER TABLE jobs ADD COLUMN agent_id TEXT;
ALTER TABLE jobs ADD COLUMN claimed_at TEXT;
ALTER TABLE jobs ADD COLUMN produced_by TEXT;
CREATE INDEX jobs_agent ON jobs (agent_id, claimed_at) WHERE agent_id IS NOT NULL;
