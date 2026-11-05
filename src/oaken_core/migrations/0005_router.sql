-- The model router: validated answers, and a ledger of what each call cost or saved.
--
-- answer_cache: one validated answer, keyed by everything that could change it. ``model`` is the
-- effective model (a changed model is a new key). The result can hold text derived from an item,
-- so the table is as sensitive as the rest of the store.
CREATE TABLE answer_cache (
    analyzer     TEXT NOT NULL,
    version      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    model        TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    tokens_in    INTEGER NOT NULL,
    tokens_out   INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (analyzer, version, content_hash, model)
);

-- llm_ledger: counters per analyzer, model and backend. Never holds text. ``tokens_saved`` is what
-- the cache and the shortcuts kept from being spent (what the stored answer cost to make).
CREATE TABLE llm_ledger (
    analyzer       TEXT NOT NULL,
    model          TEXT NOT NULL,
    backend        TEXT NOT NULL,
    calls          INTEGER NOT NULL DEFAULT 0,
    tokens_in      INTEGER NOT NULL DEFAULT 0,
    tokens_out     INTEGER NOT NULL DEFAULT 0,
    cache_hits     INTEGER NOT NULL DEFAULT 0,
    shortcut_hits  INTEGER NOT NULL DEFAULT 0,
    escalations    INTEGER NOT NULL DEFAULT 0,
    tokens_saved   INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (analyzer, model, backend)
);
