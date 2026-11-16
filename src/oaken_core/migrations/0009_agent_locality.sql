-- Where an agent runs, as the person states it when creating the token, in the router's words.
-- Only a ``local`` agent may be handed confidential content; the strictest value is the default,
-- so every agent made before this migration is ``hosted``.
ALTER TABLE agents ADD COLUMN locality TEXT NOT NULL DEFAULT 'hosted'
    CHECK (locality IN ('local', 'lan', 'hosted'));
