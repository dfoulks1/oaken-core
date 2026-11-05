-- llm_spend: tokens spent and jobs held back, per analyzer per UTC day. It is what a ``daily_tokens``
-- budget on an analyzer's route is checked against. Never holds text.
CREATE TABLE llm_spend (
    analyzer  TEXT NOT NULL,
    day       TEXT NOT NULL,
    tokens    INTEGER NOT NULL DEFAULT 0,
    deferrals INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (analyzer, day)
);
