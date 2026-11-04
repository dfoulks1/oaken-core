-- Jobs, queue state and schedules. A job's queue is decided when it is enqueued, from the
-- subject's `queue` label (or named directly); the column is what claim filters on.

CREATE TABLE jobs (
    id               TEXT PRIMARY KEY,
    subject          TEXT,
    queue            TEXT NOT NULL DEFAULT 'default',
    kind             TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    priority         INTEGER NOT NULL DEFAULT 50,
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL,
    run_after        TEXT NOT NULL,
    locked_by        TEXT,
    locked_until     TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    dedupe_key       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    parent_job_id    TEXT REFERENCES jobs (id),
    actor_kind       TEXT NOT NULL DEFAULT 'system',
    actor_label      TEXT NOT NULL DEFAULT '',
    result_json      TEXT,
    completed_by     TEXT,
    requeued_at      TEXT,
    requeue_count    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX jobs_claim ON jobs (queue, status, priority DESC, run_after);
CREATE INDEX jobs_dedupe ON jobs (dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX jobs_subject ON jobs (subject);

CREATE TABLE queue_state (
    queue      TEXT PRIMARY KEY,
    paused     INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE schedules (
    name         TEXT PRIMARY KEY,
    subject      TEXT,
    queue        TEXT NOT NULL DEFAULT 'default',
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    spec_kind    TEXT NOT NULL,
    spec         TEXT NOT NULL,
    priority     INTEGER NOT NULL,
    next_run     TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
