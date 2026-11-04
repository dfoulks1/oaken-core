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

## The design, as the owner decided it
- Labels and the change log are core, keyed to a generic `SubjectId`.
- One `default` queue; more by `new_queue(name, description, ...)`. Jobs never choose a queue.
  Intake is the only queue triagers run in. A triager writes the reserved label `queue` (and
  `priority`); core routes by it. Triage runs before ingestion, so rejected items are never stored.
- A broken triager means intake backs up. There is no fallback queue. `queue.requeue` is the way out.
- Console: loopback only, no login, never switched off, baked into core. Auth guards the API.
- Human-only operations are `mcp="never"` over MCP; the config file may lift one.

## Step 1: do this
1. A migration runner over `oaken_core.db` that applies core's migrations then each plugin's, in
   one order. Core tables: `change_log`, `labels`, `jobs` skeleton.
2. Change log: append-only (who, what, subject, before, after, time). Writes go through one function.
3. Labels: key-value on a `SubjectId`. `BaseLabel.new`, `ReservedLabel.new`, `LabelMaker`
   (a core service), `LabelError` (a bad plugin write raises it and the plugin is disabled).
   Reserved core keys: `queue`, `priority`, `confidential`. A later write replaces the value.
4. Tests for each. Property tests for: every label write has a log row; `queue` must name a
   real queue.

## Rules
- Hemingway output, smallest complete change, name what you skipped. They hate big tables and
  walls of text. Walk one topic at a time.
- Keep SPDX headers on lifted files. Never add an oaken dependency.
- Ask the owner about anything unclear instead of guessing. Put questions in a separate block at the
  end: numbered, one bold line each, your default beside it.
- Treat `tests_to_port/` as a to-do, not a failure.
- Run: `uv run --python 3.12 pytest -q` (Python 3.11 or later; this machine's default is 3.10).

## Open (ask, don't assume)
Router in core? Reject retention? Locks in core? `oaken-core` name collision with oaken-mail's
bundle package. PyPI name unchecked. Report what you skipped and what you did not verify.
