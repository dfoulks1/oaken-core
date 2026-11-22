# Changelog

## 1.0.0

The first release of Oaken Core as its own project: the pipeline and plugin loader taken out of oaken-mail and made generic. It replaces the
old `oaken-core` bundle (0.x), which is retired along with `oaken` and `oaken-cli`.

- **Pipeline:** Intake (providers submit, formatters split into parts), triage before storage, an ingestor and a store plugin, labels,
  queues and stages, jobs with leases and retries, a scheduler.
- **Plugins:** a host with plugin kinds, templates (many instances of one plugin), tags, versions, API and core version ranges;
  `plugin add`, `plugin remove`, `plugins --kind --tag`; plugins document themselves.
- **Doors:** the `oaken` CLI, a local HTTP API, an MCP server, and a console (loopback only) that draws every plugin's settings.
- **Models:** a router with local, LAN and hosted backends, an answer cache and a ledger; confidential items stay local.
- **Products:** `identity.configure(app, title, prog, daemon)` lets a package wrap core under its own name and home.
- **Operations:** `plugin_choices` (what an instance offers to pick from, such as a backend's models), `item_rerun` (now registered, and takes a batch),
  `item_enrichments`; an enrichment records how long its enricher took. A rerun gives enrichers the item's text.
- **Enrichers:** `when` and `unless` in an enricher's table choose which items it runs on (a bare name is a label or tag; `key=value` is exact).
- **Agents:** an agent can be scoped to queues (`[swarm] allowed_queues`) with no job kinds; `job_claim` takes `fields` to return only some payload keys;
  an agent's result gets the kind's own follow-up (`JobRunner.adopt`). A confidential item never goes to an outside worker.
- **Queues:** a user's request runs next on a paused queue; idle workers no longer scan every job payload.
- **Fixes:** an analyzer's `route.input_tokens` is now applied (it was read and ignored). An agent with no kinds is offered only the jobs whose
  result core can act on, and naming a kind no longer widens that. The confidential check on an agent's claim also reads a payload's own
  `confidential` flag. A label an enricher sets travels with the item to later stages. Narrowing an agent with kinds to a queue needs no
  `allowed_queues` entry, as in 1.0.0.
- **Plugins:** a plugin declares an icon; core built-ins set theirs. LLM backends list models from `available_models()` and answer a `models` command.
  Template plugins take a three-level command path (`NOUN NAME CMD`).
- **Starting config:** on first run core writes a commented `config.toml` (every default, plus an `[extensions] dirs` example) if none exists; an existing file is never touched.
- **CLI and doors:** `--oaken-home PATH` pins the home for one run. `[console] enabled = false` serves no console. The MCP server is named for the product
  (`identity.app`).
- **Router (security):** `locality = "local"` is no longer taken on the owner's word alone. An LLM backend or embedder may say, once started, that it sends
  text to another machine (`off_machine = True`, new on `BaseLLMBackend` and `BaseEmbedder`); the router then treats it as `hosted` and logs an
  error, so a confidential item cannot reach another box through a mistyped locality. A plugin that does not say keeps the owner's word.
- **Pipeline:** intake recognizes an Oaken envelope; store records, enrich and act contracts with their stage loops; LLM enrichment; `Query.order_by`.
- **Agents:** an agent now has a `locality` (`local`, `lan` or `hosted`), stated by the person in `agent_create` and shown by `agent_list`.
  It defaults to `hosted`, the strictest, and existing agents become `hosted`. Only a `local` agent may be handed confidential content. New
  `oaken_core.confidential` holds the one rule for what is confidential and who may see it (migration 0010).
- **MCP:** a confidential gate on both MCP doors. A caller now carries a clearance for confidential items, set by its door: the person's
  `/mcp` door only with the new `[api] mcp_confidential = true` (default `false`), the agent door only for a `local` agent. An uncleared
  client gets `confidential_withheld` from `item_show`, `item_get`, `label_get` and a `label_set` proposal on a confidential item, and
  `label_find` and `log_list` leave such items out. Plugin commands reach an uncleared client only if they declare
  `@command(..., confidential_aware=True)` and honour the new `PluginOpContext.confidential`. **Behaviour change:** an MCP client that read confidential items before needs the setting (or a
  `local` agent), and plugin commands that do not declare `confidential_aware` drop out of an uncleared client's tool list.
- **Swarm:** `job_claim` no longer hands a confidential job to an agent that is not `local`. The claim skips, in its own transaction, a job
  whose subject's effective `confidential` label is `true` or whose payload says it is confidential; the job waits for a local agent or
  core's own workers. Before, any agent allowed a kind received every job of it, confidential or not.
- **Fixes from the pre-1.0 code review:**
  - A schedule that cannot fire is logged and postponed, and never stops the scheduler or the other schedules.
  - A retry after a later intake step failed no longer stores the item twice. A kept reject is stored but not enriched or acted on.
  - A job finishing in a queue that is not an enricher stage starts the stages over instead of raising.
  - Workers no longer lose wake-ups, and a storage error no longer ends a worker.
  - A sweep clears a label whose key is no longer defined, so a reject can finish.
  - The labels a triager sets now reach the triagers after it and the stages after triage, not only the store. A rule that labels an item
    `confidential` now protects it from a later model call.
  - `hops` in an envelope must be a whole number and at most 8. The rate limit no longer keys on the caller-set `X-Oaken-Client` header.
    Two starting cores cannot both take the runtime file (`flock`). `*/2` in a cron day field is no longer a restriction. A bad
    `Last-Event-ID` is not a 500.
  - **Router:** a cache or vector store that is not local (`locality`, or `off_machine`) never holds a confidential item's answer or vector.
    **Behaviour change:** with such a vector store a confidential call is refused with `ConfidentialRefused`; its answers use core's cache.
    Confidential answers have their own near-duplicate collection. A bad `concurrency` in a backend's table counts as 1 and no longer
    breaks every call.
  - **Queues:** `queue_rename` carries the plugins pinned to the stage (`stage = ...`) and schedules with it. `queue_delete` is refused while
    a plugin or schedule names the queue. `job_list` leaves out a confidential item's jobs, and `queue_list` and `queue_get` hide
    `last_error`, for a caller not cleared for confidential items (an MCP client).
  - A low-confidence answer that could not be escalated (over budget, or the bigger backend is down) is no longer cached for good: it
    is answered but not stored, so the escalation happens once it can.
  - **Trace:** with `[log] trace` on, the prompt, text and answer of a confidential item are left out (`<withheld: confidential>`); the
    call, its plugin, its time and its error are still recorded.
  - Plugin status details (`plugin_list`, offered over MCP) are scrubbed of secrets like a job's last error.
  - Typing words into a number box in the console shows a message instead of "something went wrong inside core".
  - An extension directory or its `src/` that anyone may write to is skipped, not only a root that is.
