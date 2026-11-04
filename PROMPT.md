# Build oaken-core (handoff prompt)

AI-generated, unreviewed. Read `PLAN.md` first, then this.

You are continuing work in `~/local/oaken-core`. Step 0 is done (see PLAN.md). Build step 1, then
stop and report. Do not commit or push: the ASF policy needs the owner to confirm the
`Co-Authored-By` trailer first (configured string: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`;
never invent another).

## What oaken-core is
An async pipeline and a plugin loader. It has no nouns. oaken-mail and oaken-grove are plugin sets
that `import oaken_core`. Grove's spec is `~/local/oaken-grove/SPEC.md` (draft 0.7, parked); its
sections on plugins, stages, `needs`, labels and the form engine are now core's job. oaken-mail
(`~/local/oaken-mail`, Apache-2.0, "inspiration, not gospel") is the source of lifted code.

## The design
`PLAN.md` holds the decisions. In short: labels and the change log are core, in two layers (human
value, machine value; effective = human else machine; no locks). Queues are named, jobs never pick
one, a triager labels `queue`, triage runs before storage. Storage is a `BaseStore` plugin handing
core a `Database` (SQLite default, in this package). LLM backends are named plugin instances behind
a router in core. Console: loopback only, never off. Core has no nouns.

## Step 1: do this
1. `Store` protocol and a `SqliteStore` default (wraps `oaken_core.db`). Core opens its tables
   through it.
2. Migration `0001`: `change_log` (append-only, enforced by triggers) and `labels`
   (`subject_id, key, layer, value, actor, at`, one row per layer).
3. `ChangeLog`: one append function, used in the same transaction as every label write.
4. Labels: `BaseLabel.new`, `ReservedLabel.new`, `LabelMaker`, `LabelError`. Reserved core keys:
   `queue`, `priority`, `confidential`. A key must be registered before it is written. A key a
   plugin registers must not collide with a reserved one. The `queue` key takes a validator hook
   (it must name a real queue; queues arrive in step 2).
5. Tests: every label write has a log row; the log refuses update and delete; a machine re-run
   never changes the effective value while a human value exists; clearing the human layer reverts
   to the machine value; clearing the machine layer keeps the human one; unknown and colliding
   keys raise `LabelError`.

## Rules
- Hemingway output, smallest complete change, name what you skipped. They hate big tables and
  walls of text. Walk one topic at a time.
- Keep SPDX headers on lifted files. Never add an oaken dependency.
- Ask the owner about anything unclear instead of guessing. Put questions in a separate block at the
  end: numbered, one bold line each, your default beside it.
- Treat `tests_to_port/` as a to-do, not a failure.
- Run: `uv run --python 3.12 pytest -q` (Python 3.11 or later; this machine's default is 3.10).

## Open (ask, don't assume)
Efax open items and reject retention (see PLAN.md). The `oaken-core` name collision with oaken-mail's
bundle package. PyPI name unchecked. Report what you skipped and what you did not verify.
