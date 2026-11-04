-- Core's own tables. The change log is append-only: the triggers refuse update and delete.

CREATE TABLE change_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,  -- the true order; ids and timestamps can tie
    id         TEXT NOT NULL UNIQUE,
    ts         TEXT NOT NULL,
    actor_kind TEXT NOT NULL,
    actor      TEXT NOT NULL DEFAULT '',
    subject_id TEXT NOT NULL,
    key        TEXT NOT NULL,
    layer      TEXT,
    op         TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT
);
CREATE INDEX change_log_subject ON change_log (subject_id, seq);

CREATE TRIGGER change_log_no_update BEFORE UPDATE ON change_log
BEGIN
    SELECT RAISE(ABORT, 'change_log is append-only');
END;

CREATE TRIGGER change_log_no_delete BEFORE DELETE ON change_log
BEGIN
    SELECT RAISE(ABORT, 'change_log is append-only');
END;

-- One row per (subject, key, layer). The human layer wins when both exist.
CREATE TABLE labels (
    subject_id TEXT NOT NULL,
    key        TEXT NOT NULL,
    layer      TEXT NOT NULL CHECK (layer IN ('human', 'machine')),
    value      TEXT NOT NULL,
    actor_kind TEXT NOT NULL,
    actor      TEXT NOT NULL DEFAULT '',
    at         TEXT NOT NULL,
    PRIMARY KEY (subject_id, key, layer)
);
