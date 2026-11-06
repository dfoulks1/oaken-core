# oaken-core plan

AI-generated draft, unreviewed by a human. Core is an async pipeline and a plugin loader. It has no
nouns: no message, no person. Products (oaken-mail, oaken-grove) are plugin sets that import
`oaken_core` as a package. Grove's parked spec is `~/local/oaken-grove/SPEC.md`.

## Done
- Step 0 (commit 5281a70): the clean oaken-mail kernel (cf3b187) lifted, imports renamed to
  `oaken_core`, 106 lifted tests pass on Python 3.12. `ids` uses a generic `SubjectId`.
  Registry, gate and guard tests are ported (`tests/test_registry.py`, `test_gate.py`, `test_guard.py`).
- Step 1 (f913b6a): store seam, append-only change log (ordered by `seq`), two-layer labels.
- Logging (00d89a8): stdlib `logging`, five levels, per-logger levels, no structlog.
- Step 2: `jobs.py` (queues, claim, lease, retry, requeue, runner per queue), `queues.py`,
  `scheduler.py` (lifted, `subject` + queue), `intake.py` (triage before storage, reject keeps a
  content-free log row, accept moves labels to the real subject). 168 tests pass.
  Also built (owner: needed for MCP parity and for trace): external agents (token hashed, scoped
  by job kind and queue, lease and hourly quotas, revocable; `SwarmSettings` off by default and
  kinds opt-in), agent leases in `claim`, and the event bus with the event log, the SSE ring and
  the trace (lifted; mail events dropped). Job state changes publish `JobStateChanged`.
  Skipped on purpose: per-kind pause (replaced by per-queue pause), the operations themselves
  (step 4, they need the registry), and moving already-queued jobs when a `queue` label changes
  (use `requeue --to`). `oaken_core.new_queue` is `Queues.new_queue` on a registry, not a global.
- Step 4: operation layer (4a), HTTP and MCP (4b), CLI (4c, below). The CLI is built from the
  registry at start, so a plugin's verbs appear when it loads and a failed plugin has none; flags
  come from input schemas; a change is a proposal until `--yes`; exit codes are a contract
  (0 ok, 1 error, 2 usage, 3 core down, 4 auth, 5 needs confirmation).
- Step 3: the plugin host (`plugins/host.py`, lifted and reshaped), `kinds.py` (registrable kinds,
  eleven shipped), `contracts.py` (generic half), `base.py` (`BasePlugin`, `@command`), `validate.py`,
  `ui.py` (`check_ui`, lifted), `jobs.py` and `ops.py` (lifted bridges). The store is a plugin and
  starts first (`start_store`, then core's migrations, then the rest). Named instances come from
  config tables with `plugin = "<name>"`. A plugin may own tables (`migrations_dir`, its own version
  table, once per plugin) and define labels. A bad label write or a `needs` cycle switches the plugin
  off; `requires_core` is enforced. 209 tests pass. Skipped on purpose: per-plugin `capabilities`
  allow-lists (mail's provider idea), the `trial` wizard, per-method tracing wrappers, mail's
  declarative `PluginTable` store (plugins get `ctx.db` and a migrations folder instead; shortcut: not
  sandboxed), and `ctx.llm` (step 5). The registry still carries mail's `marks_read` (step 4 drops it).
- Step 5: the model router (`oaken_core/router/`). `Router.ask(route, call, request, output)` runs the
  ladder: answer cache, near-duplicate shortcut (`needs_vec` only, via `embed` and `nearest`), a
  capped small-model call, one escalation on low confidence (`confidence` field) or a bad answer.
  Backends are `[plugins.llm_backend.<name>]` tables (`plugin`, `locality`, `concurrency`; unset
  locality means hosted); an analyzer's `[...<name>.route]` table names backend, model, `fallback`,
  `escalate`. Each backend has its own semaphore. Confidential: local always, lan if in
  `[router] lan_allowlist`, hosted never; a disallowed backend is skipped, none left raises
  `ConfidentialRefused`. Cache key (analyzer, version, content hash, effective model), table
  `answer_cache`; ledger `llm_ledger`, reported by operation `ai_usage` (mcp off). `BaseLLMAnalyzer.ask`
  (triager, enricher, action bases). `Verdict.learn` and the `nearjunk` triager (rank 40, entry
  point); the junk index is off unless `[router] junk_index`. Stubs: `router/stub.py`. 274 tests pass.
  Skipped on purpose: a human decline adding its vector; moving the embedder and vector-store
  contracts' real plugins (plugins repo); a wait state for `needs_vec` jobs (the call raises
  `Unavailable` and the job retries with backoff); per-call trace of the ladder.
  Added after step 5 (uncommitted at the time of writing): `BaseLLMAnalyzer` and `BaseVecAnalyzer` are mixins
  beside a kind's base (`class Mine(BaseLLMAnalyzer, BaseTriager)`); `BaseAnalyzer` is deterministic.
  A job that needs a thing that is down or spent raises `Deferred(retry_after)`: it goes back to the queue,
  its attempt is not counted, it never fails (`needs_vec` with no embedder or vector store waits this way;
  `[router] wait_seconds`). An analyzer's route may set `daily_tokens` (0, the default, is no budget):
  once today's (UTC) spend reaches it, a model call is deferred until midnight, an escalation is skipped,
  and cache and shortcut hits stay free. Table `llm_spend`, reported by `ai_usage` as `spent_today`.
  Input budgets were already built (`input_tokens`, cut to the backend's `context_window`).
  The Ollama embedder and LLM backends were lifted from oaken-mail into core, then (2026-11-06) moved to `oaken-plugins`
  (`embedder_ollama`, `llm_backend_ollama`, `llm_backend_ollama_remote`): core ships the model plumbing (router, cache, ledger,
  `sqlite-vec`, the contract suites in `oaken_core/testing/`) and no model, so every model feature waits for a plugin. The client was verbatim apart from
  renames; the plugin takes its model from its own `model` setting (mail read `ai.embed_model`) and no longer
  refuses a remote URL itself (the router's `locality` decides who may see what). `BaseEmbedder` is new.
  `oaken_core/testing/contracts.py` starts the contract suites with the embedder's. The LLM backend (`oaken_core/ollama/llm.py`,
  `BaseLLMBackend`) is lifted too. Not lifted yet: the live test.
  Intake is wired into the daemon: it is built in `Core.open`, registered on the runner, handed the
  router, and asks the host for the healthy triagers on every item (so one that starts or stops counts
  from the next). A new exclusive kind, `ingestor` (`BaseIngestor.ingest(item) -> SubjectId`), is where an
  accepted item is stored: core has no nouns, so a product supplies it. With none running an accepted item
  is `Deferred` (waits, never fails, `[router] wait_seconds`) and flows once one starts. `ctx.submit(source,
  body, labels=...)` is a plugin's way into intake (actor `system`, labelled with the plugin's name).
  `oaken --help` lists core's own commands from the registry when no daemon is running.
  Unmeasured: the savings, until a real backend runs.
- Queues (2026-11-06): `Jobs` is the store (was `JobQueue`, `svc.jobs`); `Queue` is one queue (was `QueueSpec`) and
  takes a job: `queue.submit(JobSpec(...))`; `Queues` is the registry. Operations `queue_new|get|edit|rename|delete|
  submit|pause|resume` (CLI `oaken q ...`); new, edit, rename and delete write `[queues.<name>]` in config.toml and
  reload, and refuse `default` and queues a product makes in code; rename and delete refuse while jobs are queued
  or running. The stage page has Start/Stop and a settings dialog (edit, rename, delete).
- Extensions (2026-11-06): `[extensions] dirs = [...]` names directories of plugin directories (`~/local/oaken-plugins`:
  `oaken-<kind>-<name>/pyproject.toml` + `src/`). `PluginHost` reads each pyproject's `oaken_core.*` entry points (a
  group of another Oaken, `oaken.providers`, is ignored), puts `src/` on `sys.path`, and loads them like installed
  ones; a world-writable root is skipped. `oaken plugin add NAME|CLASS` is a thin handle on `host.plan_add` and
  `apply_add`: NAME is a plugin name (installed or in a directory) or a directory name; CLASS is `pkg.mod.Class`
  (kind from the class's `kind`, or `--kind`; persisted as `class = "mod:Class"`, which the host loads at start).
  It writes `[plugins.<kind>.<name>]` first, starts the plugin now when it is loaded, and reloads core when it
  has to find it. Not built: installing a plugin's own dependencies, `extension_list`, `git pull`.
- Built-in intake (2026-11-06): core ships what makes intake work with nothing else installed, as ordinary plugins (switch off, replace, configure): triagers `llm` (a template: instances ask a model about criteria, so it is there to receive any model backend), `dedupe` (rank 1; drops a repeat by the provider's `key`, a configured body field, or a body hash; keys only, table `dedupe_seen`) and `rules` (rank 5; the owner's ordered rules), provider `socket` (core's one provider, the machine's door: off until `unix_path` or `port` is set, made to be fed by `oaken-plugins`; the by-hand `jsonl` is in `oaken-plugins`), ingestor `items`. `BaseProvider.submit(..., key=)` carries the source's own id to `IntakeItem.key`. Code in `oaken_core/triage/`, `oaken_core/providers/`.
- Step 6: the console (`oaken_core/console/`). Core's own, no plugin involved: its own listener on a
  hard-coded 127.0.0.1 (`[console] port` only; `extra="forbid"` refuses a host), its own unit in the
  daemon, started before the host so a failing plugin cannot take it down. Guard: Host (name and
  port), loopback peer, Origin and Sec-Fetch-Site on any post, form content type, CSRF token on every
  post, a CSP with no script. Server-rendered Jinja (new dependency `jinja2`, plus explicit
  `python-multipart`). Pages: overview, plugins, a plugin's settings modal, plugin pages, queues,
  config; JSON beside: `/json/status|plugins|queues|config` and `/json/plugins/<kind>/<name>/ui`
  (the `UiSpec`, or the schema when the spec is absent or refused). A mutate shows its proposal and
  waits for a second click (the same gate as the CLI). Settings save writes `config.toml` (secrets
  refused, `config_model` checked) and restarts only that plugin. `console_port` is in `runtime.json`.
  `BaseAuth` and `remote_ok` stay on the API door. 283 tests pass.
  Skipped on purpose: lifting oaken-mail's templates (all mail copy; written new and small), the
  pipeline and stage views (nothing to show until a product has stages), editing core's own sections
  of `config.toml` from the console (read-only page), pause and requeue buttons on the queue page, and
  live updates (no script, so no SSE; reload the page).
  Not verified: any real browser (tests drive the ASGI app and one real loopback socket); the
  Origin `null` allowance is lifted as-is from oaken-mail and rests on the CSRF token.
  Added later: the console edits core's own sections (`log`, `jobs`, `swarm`, `router`, `console`) through
  operations `settings_get` and `settings_set` (CLI: `oaken settings ...`). `settings_edit.py` is the allowlist
  (lifted in shape from oaken-mail): a key that opts an outside worker in, widens who may see confidential text
  or moves a port is shown and set by hand. A change is compared with what the FILE holds (a hand edit not yet
  reloaded may differ from the running core), the whole file has to load before anything is written, and only
  the differing keys are written. `core_reload` (the console's Reload core, an operation like any other) asks the
  daemon to rebuild on `config.toml` half a second later, so the page that asked is answered first; a reload
  keeps the console on the same port. `plugin_test` (the settings modal's Test button) tries the form's values
  on a throwaway copy of the plugin via `host.probe`. Dropped: oaken-mail's `env_overrides` (core's loader reads
  no environment).
- The unread parts were read for sizing (see Sizing).

## Decided
- **Core** = async pipeline + plugin loader + operation registry + kind contracts + labels + change log.
- **Labels** are key-value on a `SubjectId`, in two layers: a human value and a machine value.
  Effective = human if set, else machine. Humans write the human layer (actor kind `user`),
  machines the machine layer (`agent`, `system`). No locks, no pending changes, no skipped writes.
  Clearing the human layer is the unlock. `--retriage` clears the machine layer. Both layers and the
  actor are logged. The change log is append-only.
- **Queues:** one `default`; more by `oaken_core.new_queue(name, description, ...)`. Jobs do not
  pick a queue. A triager writes the reserved label `queue`; core routes by it. Scheduled and
  maintenance jobs name their queue directly.
- **The intake chain** (2026-11-06) is a row of stages, each a queue, routed by the `queue` label (a stage's name is its
  `[queues.<name>]` key): **Intake** (`default`, titled Intake: formatters split the raw item into named parts and label
  `format` = ok / none / unmatched / failed) -> one or more **triager stages** (a triager runs in the stage its `stage`
  setting names, else the first; several stages may share a kind; `[queues.<name>] disabled` switches a plugin off in one
  stage) -> hidden **`store`** -> hidden **`embed`**. Triage runs BEFORE ingestion, so what is stored is up to the labels:
  `store` (true/false, else `[jobs] store_by_default`; a reject is stored only with `store=true`) and `embed` (else
  `[jobs] embed_by_default`, off). Intake labels an item `triage=pending`; the stage that decides clears it. A failing
  triager lets its stage back up (no fallback queue). `[jobs] unmatched_format` / `failed_format` (pass | fail) say whether an
  unformatted item goes on whole or stays in Intake's failed jobs to requeue. A reject keeps a content-free log row only.
  Providers (`socket`: the machine's door, `jsonl`: by hand) only provide; Intake formats and verifies. A reserved label can be
  made by a plugin or the owner and may carry `writers` (who may set it). Plugins: `oaken plugin add NAME` looks in the
  environment and `[extensions] dirs`, then in `oaken_plugins` (`oaken plugins` lists it); `json` ships in core.
  The log is kept (2026-11-06): every line is a `LogEvent` on the bus (streams only, like the trace; the default stream and the event
  log never see it). With no `[log] file` a writer stream keeps them in the `log_lines` table (`[log] keep_days`, default 7); with a
  file, the file is the log. On unless `[log] enabled = false`. `log_tail` (`oaken log tail --plugin kind/name | --stage queue`) reads
  it in widening scope; plugin, stage and General pages have a folded Log panel. Not built: `oaken log tail -f` (the bus has it).
  Not built: strict/permissive `mode` on every stage; parts as child subjects with `reads` / `ctx.content`; a batch importer.
- **`queue.requeue`** (job or selector, `--to <queue>`, `--retriage`): `mcp="never"` by default,
  a config setting may lift it. Built on oaken-mail's `retry_job`.
- **Storage** is local and provided by a `BaseStore` plugin. A store hands core a `Database`
  (execute, fetchone, fetchall, `transaction()`, `migrate(dir)`); core and plugins write portable
  SQL. SQLite ships in the core package as the default first-party plugin. A Postgres store is
  possible later. Load order: settings, store, then the rest of the host.
- **LLM backends** are plugins, many at once, as named config entries (plugin, URL, `locality` of
  local | lan | hosted, `concurrency`). The model router lives in core. Each analyzer names a backend
  and model, with an optional ordered fallback list. `confidential` only routes to `local` unless a
  backend is allowlisted; the router refuses rather than fall back to a remote box. A bridge is
  just another backend (an OpenAI-compatible plugin covers LiteLLM, vLLM, LM Studio, OpenRouter).
  Hosted backends live in a plugins repo, not core. The host's `<plugin>@<id>` instances (built for
  mail accounts) are generalized to named instances.
- **Generic enough:** plugin kinds are registrable (`register_kind`), not a closed list; an analyzer's
  input is an opaque view the product builds (core assumes no text); a view supplies its own
  content hash; `needs` relations are `self`, `parent`, `children` plus ones a product registers.
- **Console:** baked into core, loopback only, no login, never off, fixed bind. It serves config,
  plugins, queues, health and every plugin's `UiSpec`. Core serves `UiSpec` as JSON plus a minimal
  Jinja fallback. `BaseAuth` guards the API/MCP/gateway endpoint, where `remote_ok` lives.
- **MCP access uses the agent-token mechanism.** An MCP client is an agent: same hashed, revocable
  token, same quotas. Its actor is `agent`, so anything it writes to a label lands in the machine
  layer. Step 4 adds a per-agent list of operations it may call; the `mcp="never"` ceiling stays
  above it. Agent leases are a separate path from LLM providers: a provider is a plugin the router
  calls; a lease lets a client that is the model (Claude, Librarian) do a job itself.
- **Events:** the bus is for job state, plugin health, live console and MCP updates, trace
  (opt-in, full text, never persisted by the event log). Logging does not need it (stdlib
  `logging`); a small handler can forward records to the bus for live tail.
- **The CLI is built at plugin load.** Core generates it from the operation registry, and a plugin's
  operations (and its command groups, such as `grove people add`) appear as the host loads it. A
  plugin that fails to load drops its commands and the CLI still starts.

## Open (proposed defaults, not yet answered)
From the efax thought experiment: payloads as blob references (not inline queue rows); a "parked,
awaiting" job state with a resume key; provider push mode with its own signature check; the
conformance kit gets two fake products (a feed reader and efax) as the generic-ness test.
Also: `confidential` settable at intake; reject retention as config.

## Sizing (read from oaken-mail cf3b187)
- Lift with light surgery: `plugins/host` (generalize account instances), `JobRunner`,
  `pipeline_control` (becomes per-queue pause and limits), `daemon/lifecycle`, `units`.
- Lift about half: `plugins/contracts` (generic: context, health, operations, UiSpec, job handlers,
  scheduled tasks, LLM, cache), `plugins/base` (BasePlugin, LLMBackend, Analyzer),
  `JobQueue` (`account_id` becomes `subject`, add a queue column).
- Rewrite: `ingest/pipeline` (about 600 lines new; keep its patterns: one job kind per plugin with a
  `dedupe_key`, a lazy triager chain by rank, a result cache keyed by content hash, name, version
  and model with agent rows counting as current, a `min_priority` bar), `daemon/core`, `events`.
- Medium, not small. Unread: pipeline agent/reprocess code, `base.py` triager/action bodies.

## AI cost (owner: this must optimize AI usage; it is the model router's job)
The router owns the whole ladder below, including the embedding shortcuts: it is the one place that
decides the cheapest way to answer a call, records what it spent, and exposes `embed` and `nearest`
to analyzers that declare `needs_vec` (they never touch the vector store directly).
Cost ladder, cheapest first: rules (0 tokens), cache (0), embedding shortcuts (near-duplicate reuse of a
neighbour's stored result, nearest-example triage), a small local model with structured, length-capped
output, then escalate to a larger model only on low confidence or a failed schema check. Per-analyzer
input budgets (cut to the backend's `context_window`). A usage ledger per analyzer, model and backend
(tokens in and out, cache hits, escalations), because "optimized" needs a number. Vectors are never
sent to a model: they find which text to send or whether to skip the call. Step 5 builds the router,
cascade, budgets and ledger; the embedder and vector store get their first consumer (near-duplicate
reuse, nearest-example triage) right after, which settles spec open items 5 and 6. Unmeasured.

## Vectors and junk (owner idea; part of the model router)
`needs_vec` pairs with `needs_llm`: the job calls the embedder and vector store, runs in core only
(never an agent's) and waits for both to be healthy. Triage gets a fourth outcome, reject and learn:
junk no triager recognizes is embedded and nothing else (no model, no enrichers, nothing stored). A
cheap `nearjunk` triager (`needs_vec`, rank between `rules` and `ai`) embeds an incoming item and
rejects it when a close neighbour is in the junk index, with no model call. A human decline can add
its vector too. Privacy: a vector is derived from the content and can be partly inverted, so the
junk index keeps the vector and a hash only, behind a config switch that defaults off. Not built:
the `learn` field waits for the embedder and vector store (step 5 and after). `needs_vec` is on the
analyzer bases, validated, and excluded from `agent_eligible_kinds`.

## Build order
1. Store seam (`Store` protocol, SQLite default), migrations, change log, labels
   (`BaseLabel`, `ReservedLabel`, `LabelMaker`, `LabelError`).
2. Jobs with `subject`, named queues, label routing, intake, requeue, scheduler.
3. Plugin host (lift), kinds registry, store-first load order, named instances, `requires_core`,
   acyclic `needs` check, one migration runner across core and plugin tables.
4. Skins: generic services/client, HTTP (starlette), CLI flag generation, MCP bridge.
5. Router and backend kind, named backends, cache, `confidential` enforcement.
6. Console: `UiSpec`, checker, default modal, baked loopback renderer, Host/Origin/CSRF guard.
7. Conformance kit with fake products.

## Risks
`oaken-core` is also the name of oaken-mail's bundle package (owner will sort the name). Intake holds
raw unstored payloads in queue rows until blob references exist. Unverified: PyPI name; an
OpenAI-compatible endpoint's mapping onto `LLMRequest` (`json_schema`); SQLite-only features in
lifted code. Nothing here has run against a third product yet.
