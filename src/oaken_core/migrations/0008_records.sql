-- Records: what a store's create/get/list/update/delete work on. Core has no nouns, so a record is
-- JSON under (collection, id). ``rev`` counts writes, for optimistic concurrency.
CREATE TABLE records (
    collection TEXT NOT NULL,
    id         TEXT NOT NULL,
    data_json  TEXT NOT NULL,
    rev        INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (collection, id)
);
