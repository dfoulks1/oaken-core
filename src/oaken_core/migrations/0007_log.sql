-- The log, when no [log] file is set: every line core logs, as ``logging`` made it (already scrubbed, see logging._event).
CREATE TABLE log_lines (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    level  TEXT NOT NULL,
    logger TEXT NOT NULL,
    plugin TEXT NOT NULL DEFAULT '',  -- "kind/name" when the line came from a plugin's own logger
    queue  TEXT NOT NULL DEFAULT '',  -- the stage a job was in when it logged
    event  TEXT NOT NULL,
    data   TEXT NOT NULL DEFAULT '{}'  -- the rest of the line's fields, as JSON
);
CREATE INDEX log_lines_ts ON log_lines (ts);
CREATE INDEX log_lines_plugin ON log_lines (plugin, id);
CREATE INDEX log_lines_queue ON log_lines (queue, id);
