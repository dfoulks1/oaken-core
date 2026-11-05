-- items_rows: what the built-in `items` ingestor stores. A subject, where it came from, and the item as JSON.
-- It holds the text a person submitted, so it is as private as the rest of core's database.
CREATE TABLE items_rows (
    subject_id TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    body_json  TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX items_rows_created ON items_rows (created_at);
