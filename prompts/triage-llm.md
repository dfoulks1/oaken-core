# Context

Written with Claude (Sonnet 5.5) in a session on oaken-core, 2026-11-06, at the owner's direction ("pull the ai
triager into the extensions directory"). Modelled on oaken-mail `cf3b187`, `src/oaken_triagers/triage.py` (the
`triage` plugin: a prompt, a validated answer, rules first), but generic: the owner's `criteria` replace the mail
categories, and the verdict is core's (`accept`, `reject`, `unsure`). Unreviewed by a human until the owner reads it.
