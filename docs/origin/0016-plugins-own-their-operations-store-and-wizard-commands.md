# 16. A plugin owns its operations, its small store and its wizard commands

- Status: accepted (ratified 2026-11-03 by the owner, who verified the built implementation for 0.2.0)
- Date: 2026-11-03
- Rests on: ADR 0017 (triagers and enrichers are separate kinds) and ADR 0018 (a web UI contract for
  plugins), built first, in that order
- Amends: ADR 0012 (the plugin context gains two scoped capabilities; the no-database-handle
  boundary stays) and ADR 0015 (where the rule and level operations live, and the CLI wizard)
- Built: 2026-11-03, not yet verified by the owner's checks: the `store` (declarative, no SQL),
  `folders` and `draft` capabilities (`core/plugin_store.py`), the rules plugin's own operations
  (`rules.*`, over its staged draft) and the levels as `BaseTriager` operations over one table of the
  kind, and the removal of core's rule and level operations, `TriageStore`, `oaken rule` and
  `oaken level`. Not built: grants (mechanism unidentified), a CLI wizard that walks a page, and
  `rules.test` against stored mail (a plugin gets none, so it checks the rule as typed).

## Context

ADR 0015 built the reader's triage rules in core: the `triage_rule_*` and `triage_level_*`
operations, a store, a CLI wizard (`oaken rule`, `oaken level`) and a console section. The `rules`
triager plugin only reads the result. So with the plugin off, the commands, the console section and
the MCP tools are all still there, and they stage rules nothing runs.

The owner's ruling: "triager levels and triager rules absolutely belong bundled with the rules
plugin. that's the _entire_ point." And the contract "was designed that way to facilitate exactly
this: plugins inform the CLI so you don't need to edit a bunch of stuff, just your plugin."

Premises, each checked on 2026-11-03:

- A plugin offers operations through `operations()`; core namespaces each as `<plugin>.<name>`
  (`plugin_ops.adapt`). The registry asks the plugin source on every lookup
  (`OperationRegistry._plugin_ops`), so an operation is there while its plugin is loaded and
  "unknown" when it is not, and a reload changes the answer with no restart.
- The CLI already reaches any registry operation: `oaken PLUGIN VERB` and `oaken operation
  --describe NAME`, with flags built from the input schema (`schema_flags.py`).
- A plugin's operation context holds its `config`, a clock and a logger, and no database handle
  (`PluginOpContext`, ADR 0012). Core owns `config.toml`, its backups and the plugin reload.
- The wizard is the one thing the generic path lacks: `triage_cmd.py` walks through the fields,
  runs the dry run and confirms, all by hand in the CLI.
- `PLUGIN_API_VERSION` is 2; ADR 0010 allows new optional members within a version.

## Decision

1. **The `rules` plugin offers the rule operations itself.** They are `rules.*`, declared in
   `CustomRulesTriager.operations()`. Core's built-in `triage_rule_*` is removed. With the plugin
   off, they do not exist for the CLI, the console or MCP.
   **Levels belong to the base triager.** A level is a numerical label (a number and a name) that
   the enrichers read to make their judgements (`min_priority`). Every triager exists to set one, so
   the level
   model and its operations (list, add, remove: the reader's names for 4 and up) are part of
   `BaseTriager`, not of the `rules` plugin. Core's built-in `triage_level_*` is removed; a
   triager that inherits `BaseTriager` offers them. `spamcheck` and `rules` always ship as defaults, so
   the level operations are always there; an `ai` triager also exists and its part is not yet ruled.
   **`BaseTriager` coordinates the levels** so the enrichment jobs are queued right: it holds the one
   shared set, checks that a verdict's level is defined, and gives the level's urgency and the
   `min_priority` bar that the pipeline uses when it registers the job (today `max(0, 3 - priority)`
   in the pipeline). The plugin still never touches the queue; core enqueues, asking the base
   triager for the number. The level operations are offered once, under the triager kind, not per
   plugin.
   *Cited:* "there will always be the default triagers of `spamcheck` and `rules`. those ship default"; "triager levels and triager rules absolutely belong bundled with the rules plugin";
   "levels are a triager thing. _all_ triagers exist to set levels, this is base plugin stuff.
   well base triager anyway."
2. **The context gains three capabilities, each scoped to the calling plugin.**
   - `store`: the plugin's own tables, owned by the plugin (the rules triager's rules and levels, the
     `attachment` plugin's records). Not a handle to core's tables: it cannot name another plugin's data
     or core's. Everything a plugin owns, tables and folders alike, is visible to the end user through
     `oaken` (the CLI and the console) and to core; nothing is hidden from them. The form is
     **prefixed tables in core's database,** `<kind>_<name>_<table>`, created by core from the plugin's
     declared schema and versioned migrations, so a plugin cannot reach another's tables. Everything is
     standardized on UIDs: every table has a `uid` key (uuid7) and `created_at` and `updated_at`, and a
     reference to a message, a rule or another record is by UID, never by a bare integer. No plugin
     joins to another plugin's tables; core mediates. This is to avoid a sprawl of ad hoc schemas.
     **A plugin can be dropped without breaking anything else.** Tables are pruned without breaking
     others, because a reference across an ownership boundary is by UID alone: no `REFERENCES`
     constraint, no cascade, between one plugin's tables and another's or core's. Within one plugin's own
     tables a constraint is allowed. A reader of a UID that no longer resolves (a rule whose level is
     gone, a verdict naming a triager that is not installed) shows it as missing and carries on; it never
     fails. A purge of a plugin removes its prefixed tables and folders on the person's confirmation.
     *Cited:* "we can prune tables without breaking others, always reference by UID so that if
     necessary an entire plugin can be dropped without breaking the database."
     Whether a plugin may run its own SQL against its tables, or only a declarative interface, is open.
     *Cited:* "prefixed tables are preferred but I'd like to standardize everything where possible with
     UIDs. I don't want to end up with an 'Atlassian Database Spaghetti Mess'."
     *Cited:* "plugins need to be able to own their own tables and whatnot, attachment should own
     its folders but all of this is accessible to the end user (and oaken)." The rules plugin keeps its rule and
     level uids, order and working copy here.
   - `folders`: the plugin's own folders under `OAKEN_HOME`. A plugin declares `folders = ("name", ...)`;
     core provisions each at start under `OAKEN_HOME/plugins/<kind>/<name>/` with the mode `0700` of
     `core/paths._ensure`, and the plugin reads the paths from `ctx.folders`. It cannot name a path
     outside its own folder. A destination the reader chooses (`~/Reports`) is not provisioned: it is
     a grant (ADR 0019, 4b). The quarantine writer, moved out of core, is the first user.
     *Cited:* "plugins need to make core extensible."
   - `draft`: the staged copy of the plugin's own `config.toml` table. A plugin reads and replaces
     its draft; **core** validates it against the plugin's `config_model`, writes the file once with
     one backup, makes it the snapshot and reloads that plugin. Core also does discard, drift,
     accept and revert, for any plugin that stages. The staging, `apply` and drift rules of
     ADR 0015 (decisions 1, 2a and 8) stand unchanged; only who runs them changes.
3. **The rules wizard is a page declared through ADR 0018.** The rules triager's `ui()` declares one
   page, a new rule: fields in order, `rules.test` as its Test and `rules.add` as its submit. There is
   no `rules.wizard` operation and no wizard code of core's. The CLI walks the page when stdin is a
   terminal and fields are missing; the console serves it as a form. Core's `/triage/rules/new` and
   `triage_cmd.py` go.
   *Cited:* "the wizard belongs only to the rules triager" (it is the first user of the contract);
   "you should be able to add an endpoint and serve a form."
4. **No CLI edit per plugin.** `oaken rules add` is the generic `oaken PLUGIN VERB`. `triage_cmd.py`
   and the `oaken rule` and `oaken level` verbs go. The console's actions call `rules.*`.
5. **It is additive within `PLUGIN_API_VERSION` 2** (ADR 0010): two optional context members. The contract and operation snapshots are regenerated in the same change.

## Alternatives

- **Keep the operations in core and refuse them while the plugin is off.** Smaller, and enough for
  0.2.0. Overruled by the owner: the commands then exist without the plugin, and every future
  plugin that wants a wizard command edits the CLI.
- **Give plugins a database handle.** Breaks the ADR 0012 boundary for every plugin to serve one.

## Consequences

- Turning the `rules` plugin off removes its commands, its console section and its MCP tools.
  A plugin with `store`, `draft` and `wizard` gets staging, drift and a wizard without touching
  core, the CLI or the console.
- Core gains a generic store, a draft mechanism and the plugin web UI of ADR 0018 (no renderer of its own). `triage_store.py`,
  `builtin/triage_rules.py`, `triage_cmd.py` and migration `0018` are reworked or removed.
  Migration `0018` is not released, so it can be replaced; the owner's live `~/.oaken` rows need a
  one-time move, or the rules are re-added.
- The console's new-rule page becomes the wizard form; the rules dialog calls `rules.*`
  and the base triager's level operations.
- Plugin operations are never on over MCP by default (ADR 0012), so the rule tools are off until
  the owner turns them on. That reverses today's built-in exposure.

**Revisit when:** a second plugin wants a `store` larger than documents (a real table).

## History

- 2026-11-03: drafted from the owner's words in a working session. Not ratified. Only the owner
  ratifies.
- 2026-11-03: ratified by the owner, who verified the built implementation works for 0.2.0.
- 2026-11-03 (same session, before ratification): the owner ruled the names are fine, `guided` is
  `wizard`, the console page is a web form that can run the wizard, and levels belong to the base
  triager. Decisions 1 and 3 reworded; the first draft put levels in the `rules` plugin and called
  the declaration `guided`.
- 2026-11-03 (same session, before ratification): the owner ruled `BaseTriager` coordinates the
  levels so jobs register back to the queue for enrichment. Levels are one shared set, offered once
  under the triager kind; the open question is closed and removed.
- 2026-11-03 (same session, before ratification): the owner ruled a level is only a numerical label
  the enrichers use to judge. Decision 1 says so; the level operations stay label edits (number and
  name), and the pipeline's urgency stays a function of the number.
- 2026-11-03 (same session, before ratification): the owner ruled the wizard belongs only to the
  rules triager, and that this builds after ADR 0017. Decision 3 rewritten: no `wizard` field on
  `PluginOperation`; a `rules.wizard` read operation instead. The earlier draft made it a generic
  declaration any plugin could carry.
- 2026-11-03 (same session, before ratification): the owner ruled a plugin web UI contract goes into
  0.2.0 (ADR 0018). Decision 3 rewritten again: the rules wizard is a page the rules triager declares,
  not a `rules.wizard` operation. Build order is 0017, 0018, then this ADR.
- 2026-11-03 (same session, before ratification): the owner ruled plugins can have folders provisioned
  under `OAKEN_HOME`. Decision 2 gains `folders`; the context now has three capabilities, not two.
- 2026-11-03 (same session, before ratification): the owner ruled the rules plugin's order control is the
  orderable list of ADR 0018 (decision 4a): a position field per rule and one Update, no drag and drop.
- 2026-11-03 (same session, before ratification): the owner ruled the capabilities are kept, and widened
  `store` from documents to plugin-owned tables, with all plugin data visible to the user and core.
- 2026-11-03 (same session, before ratification): the owner ruled plugin storage is prefixed tables in
  core's database, standardized on UIDs. Decision 2's `store` says so.
- 2026-11-03 (same session, before ratification): the owner ruled the drop principle: a reference across
  an ownership boundary is by UID alone, so a plugin can be dropped without breaking the database.
