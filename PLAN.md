# oaken-core plan

AI-generated draft, unreviewed by a human. Core is an async pipeline and a plugin loader. It has no
nouns: no message, no person. Products (oaken-mail, oaken-grove) are plugin sets that import
`oaken_core` as a package. Grove's parked spec is `~/local/oaken-grove/SPEC.md`.

## Done
- Step 0 (commit 5281a70): the clean oaken-mail kernel (cf3b187) lifted, imports renamed to
  `oaken_core`, 106 lifted tests pass on Python 3.12. `ids` uses a generic `SubjectId`.
  Three tests wait in `tests_to_port/` (registry, gate, guard): they need a new test rig.
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
- **Intake** is the only queue triagers run in. Triage runs BEFORE ingestion, so rejects are never
  stored. A failing triager lets intake back up (no fallback queue). Making the last failed attempt
  open the gate, as oaken-mail does, is a config choice; the default is to back up. Core never
  auto-purges intake. A reject keeps a content-free log row only (config may change it).
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

## Build order
1. Store seam (`Store` protocol, SQLite default), migrations, change log, labels
   (`BaseLabel`, `ReservedLabel`, `LabelMaker`, `LabelError`).
2. Jobs with `subject`, named queues, label routing, intake, requeue, scheduler.
3. Plugin host (lift), kinds registry, store-first load order, named instances, `requires_core`,
   acyclic `needs` check, one migration runner across core and plugin tables.
4. Skins: generic services/client, HTTP (starlette), CLI flag generation, MCP bridge.
5. Router and backend kind, named backends, cache, `confidential` enforcement.
6. Console: `UiSpec`, checker, default modal, baked loopback renderer, Host/Origin/CSRF guard.
7. Conformance kit with fake products; port `tests_to_port/`.

## Risks
`oaken-core` is also the name of oaken-mail's bundle package (owner will sort the name). Intake holds
raw unstored payloads in queue rows until blob references exist. Unverified: PyPI name; an
OpenAI-compatible endpoint's mapping onto `LLMRequest` (`json_schema`); SQLite-only features in
lifted code. Nothing here has run against a third product yet.
