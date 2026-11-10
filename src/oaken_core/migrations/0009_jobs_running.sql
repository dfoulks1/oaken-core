-- The few jobs that are running: what a worker's idle wait (next lease to run out) and the per-kind concurrency check ask about.
-- Without it each is a scan of every job row, and a queued job's payload sits before the columns they need, so a deep backlog
-- made every idle worker read the whole table (a gigabyte) several times a second.
CREATE INDEX jobs_running ON jobs (kind, locked_until) WHERE status = 'running';
