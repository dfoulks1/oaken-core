-- dedupe_seen: one row per item already let through. Keys and hashes only, never the item.
CREATE TABLE dedupe_seen (
    source     TEXT NOT NULL,
    key        TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (source, key)
);
