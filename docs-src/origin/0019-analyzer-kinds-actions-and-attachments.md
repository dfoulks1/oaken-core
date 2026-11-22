# 19. The kinds of analyzer: enrich, triage and action, and the attachment lane

- Status: accepted (ratified 2026-11-03 by the owner, who verified the built implementation for 0.2.0; decision 5 is superseded by ADR 0020)
- Date: 2026-11-03
- Rests on: ADR 0017 (the separation, `BaseAnalyzer`, triage as a core concept)
- Amends: ADR 0012 (a third pipeline kind, with effects outside the mail store)
- Built: 2026-11-03 (decision 5 reworked the same day, see ADR 0020), not yet verified by the owner's checks: decisions 1 to 4a, 4b2 to 4b4, 4c and
  4e (the kind, `route`, `approval`, parked jobs, `action_results`, `get_action`, `list_actions`,
  `approve_action`, `oaken action`, the Action tab). Built later the same day: decision 5 (the `attachment` plugin in `oaken_actions`, the byte reader
  `PluginContext.attachments`, the quarantine writer moved out of core, the `quarantine` route
  queued by core). Not built: grants (4b), a destination the reader chooses, and 4d needs no code. Proof: `src/oaken/ingest/tests/test_actions.py`.

## Context

ADR 0017 separates triagers from enrichers under a `BaseAnalyzer` helper. The owner wants a third
kind, the action, built in 0.2.0: "the actions should be capable of supporting basic workflow
automations (save this attachment to here if it's a monthly report, or collate this data into a
spreadsheet when you get the email) all the way to handling emails for something like a help desk. I
was sent an email with this format, open a ticket with this information, to I received this
attachment via efax, I want to parse it and fill out an internal report." Attachments need "a special
triager and enrichment queue"; on how, "we'll talk about this, the point is make it as extensible
as possible."

Premises, checked 2026-11-03:

- A plugin never writes mail, marks it read or sends it (ADR 0012, `plugin_ops.py`).
- `EnrichInput` (now `CleanMessage`) carries attachment metadata only: filename and size.
- Triage already decides what is owed after it (the `min_priority` gate); CORE_51 planned rules that
  launch automations from a label and did not build them.
- Triagers and enrichers have their own job kinds, concurrency and stages (`ingest.triage`,
  `ingest.enrich.<name>`).

## Decision (ruled so far)

1. **Three kinds are supported out of the box: `enrich`, `triage` and `action`.** `BaseAction`
   extends `BaseAnalyzer`, as `BaseEnricher` and `BaseTriager` do (ADR 0017), so the analyzer
   mechanism serves all three; a further kind is a new base class and a loop, nothing
   else.
2. **A plugin has one role, taken from the base class it extends** (`BaseEnricher`, `BaseTriager` or
   `BaseAction`). A custom analysis plugin, for example custom prompt analysis, inherits all it needs
   from `BaseEnricher` or `BaseTriager`. There is no multi-role plugin. Plugins are found through one
   entry-point group, and the role is read from the class.
   *Cited:* "any custom analysis plugin for later ... will inherit everything it needs to be a
   triager or enricher from BaseEnricher or BaseTriager"; "do we want it to be both things? what
   reason could there be for a triager to also be an enricher?"
3. **Triage decides whether a message goes to an action.** The verdict carries the routing; an action
   runs only for a message triage routed to it.
   *Cited:* "triage should be the thing that determines whether or not to route a message to an
   action."
3a. **The verdict gains an optional `route`:** a list of names, empty by default (a field of the core
   verdict shape of ADR 0017, so any triager can set it: the reader's rules, and a model if the reader
   lets it). An action plugin declares the route names it answers to; core queues one action job per
   route. Additive: a verdict without `route` routes nowhere.
   *Cited:* "triagers are extensible enough to add `route:` right?"
4. **An action does real work outside the mail store,** from saving an attachment to a folder and
   filling a spreadsheet to opening a ticket and filing a report from a parsed attachment.
4a. **Approval uses the engine core already has.** Each action has `approval = "ask" | "auto"`,
   default ask. An `ask` action's job is parked "awaiting approval", and approving it is the existing
   confirmed operation on a parked job (`retry_job`, or an `approve_action` that calls it), with the
   confirm gate and its rules for who may confirm (an agent may not). No new status machine. `auto`
   runs the job. The reader sets `auto` for the ones they trust.
   *Cited:* "the approval engine is a thing."
4b. **What an action may touch is granted by the reader, through the grants core already has.** The
   action's context carries only what was granted; there is no ambient file or network access. An
   action has two kinds of folder: its own, provisioned by core under `OAKEN_HOME` (`ctx.folders`,
   ADR 0016), and a destination the reader grants by naming it in `[plugins.action.<name>]`, which
   core checks is writable at start. The grant mechanism for the second is not yet identified (open
   question).
   *Cited:* "grants are there."
4b2. **No undo mechanism.** An action should be idempotent, keyed by (message, action, route), and may
   document a suggested undo for the person to do by hand. Oaken adds no `undo()`.
   *Cited:* "I'm not sure how you'd implement an `undo` perhaps just leave that as a suggestion (or
   make the action idempotent, either way)."
4b3. **Audit goes to the trace and the logger,** not to a table: the action, route, message id,
   status and the grants used, never message content.
   *Cited:* "Audit lines can go right to trace / the logger."
4b4. **An unrouted message is parked.** When no enabled action answers a route, core queues the
   action job on a generic kind and parks it with the reason "no action answers route X". It shows in
   the job list and the Action tab and is released with `retry_job` once an action is installed.
   *Cited:* "unrouted gets parked."
4c. **An action's result is read with `get_action`.** By default it shows whether the action succeeded
   or failed; a plugin widens what it shows by listing more fields. There is no `ask` (ADR 0017, 2c), so
   no router menu and no "available to ask" switch.
   *Cited:* "you should be able to ask about the results of an action to a limited degree no? return
   success / fail"; "lets drop ask entirely."
4d. **MCP is modular and is tuned later.** A plugin's operations, an action's among them, are never
   exposed to MCP by default (ADR 0012) and are switched on per operation or per plugin as now. No new
   MCP rule is made here.
   *Cited:* "capabilities are also modular for the MCP, we can tune down its permissions later."
4e. **The console has a tab for each kind,** with Action added to the navbar beside Triage, Enrich
   and Embed. A kind's page has the kind's settings at the top (step gating, enable and disable) and
   a plugins table below, one row per plugin with Reload and Settings buttons. A plugin's own
   endpoint is reached through its settings modal (ADR 0018).
   *Cited:* "each kind gets its own tab in the UI (we'll need to add actions to the navbar). kind
   settings ... at the top with each plugin getting a row in the plugins table with a reload and
   settings button."
5. **Attachment handling is an action, the `attachment` plugin** (`[plugins.action.attachment]`),
   over a core quarantine (ADR 0020). Core moves every attachment out of `raw.db` into a mode-0000
   folder when mail is stored; a scanner (an action on the `scan` route) must say `clean` before
   anything is released. The `attachment` action's `save` route copies clean files to a folder through
   `PluginContext.attachments.copy_to`; plugins never hold attachment bytes in memory. A virus scan
   is a separate action. The parser records metadata and reads the stub. The `quarantine` route, its
   writer and the unused `AttachmentExtractor` are gone. Triage routes by the rule's `has_attachment`
   condition.
   *Cited:* "perhaps a handle attachment router? (strip it off and save it $here, it would replace
   the current attachment handler mechanism)"; "attachment for stripping attachments. action _may_
   be used to run a virus scan or something else later."

## Open questions (the owner's)

- **Which grant mechanism is meant,** its keys, and where secrets for a spreadsheet or ticket action
  live (credentials belong in the OS keyring, never in config: `docs/plugins.md`).
- **The action's store:** a results table with a status (done, failed, skipped), the error, the job
  kind and concurrency, retries, and what a failed effect leaves behind. A parked job carries
  "awaiting approval" and "no route answers"; the table need not hold them.
- **Who decides route names:** an action plugin declares them; what happens to a route no installed
  action answers to, and whether a model may invent one.
- **Attachment content:** how a plugin reads it (bytes, a path, text), size limits, where parsed
  output is stored, and the lane's job kinds and concurrency.
- **Scope for 0.2.0:** the kind, its loop, its store and one worked example, or more.

## History

- 2026-11-03 (before ratification): decision 5 rewritten in place for the core quarantine, ADR 0020. It had
  a plugin-side quarantine route and an unruled byte reader; `read` became `copy_to` plus the scan door.

- 2026-11-03: drafted from the owner's answers in a working session. Not ratified. Only the owner
  ratifies.
- 2026-11-03: ratified by the owner, who verified the built implementation works for 0.2.0.
- 2026-11-03 (same session, before ratification): the owner ruled approval is a per-action setting
  (default ask), the reader grants what an action may touch, triage routes by a `route` field, and
  attachments wait until after the action kind. Decisions 3a, 4a and 4b added.
- 2026-11-03 (same session, before ratification): the owner ruled `BaseAction` extends
  `BaseAnalyzer`. Decision 1 says so.
- 2026-11-03 (same session, before ratification): the owner ruled actions are askable for success or
  failure, third-party menu growth is handled, each kind has a console tab, and MCP stays modular.
  Decisions 4c, 4d and 4e added; the MCP open question is closed.
- 2026-11-03 (same session, before ratification): the owner ruled approval uses the existing engine, grants
  are existing, no undo (idempotent, suggested), audit to trace and logger, unrouted is parked, and
  attachment handling is an action that replaces the current handler. Decisions 4a and 4b reworded;
  4b2, 4b3 and 4b4 added; decision 5 rewritten. The earlier draft held proposals in a status column,
  had an `undo()`, and recorded an unrouted message with no job.
- 2026-11-03 (same session, before ratification): the owner named the plugin `attachment`, and said an
  action may later run a virus scan or other work. Decision 5 says so.
- 2026-11-03 (same session): the drop principle of ADR 0016 applies to the action kind: an action's
  results table references a message by UID with no constraint, a route that no installed action answers
  is parked, and dropping the `attachment` plugin leaves the rest of the database whole.
- 2026-11-03 (same session): `ask` is dropped (ADR 0017, 2c), so decision 4c no longer has an ask
  intent or a menu switch; an action's result is read with `get_action`.
