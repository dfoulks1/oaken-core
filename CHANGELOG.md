# Changelog

## 1.0.1 (alpha)

What a product with a browser UI, and the plugins, needed on top of 1.0.0.

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

## 1.0.0 (alpha)

The first release of Oaken Core as its own project: the pipeline and plugin loader taken out of oaken-mail and made generic. It replaces the
old `oaken-core` bundle (0.x), which is retired along with `oaken` and `oaken-cli`.

- **Pipeline:** Intake (providers submit, formatters split into parts), triage before storage, an ingestor and a store plugin, labels,
  queues and stages, jobs with leases and retries, a scheduler.
- **Plugins:** a host with plugin kinds, templates (many instances of one plugin), tags, versions, API and core version ranges;
  `plugin add`, `plugin remove`, `plugins --kind --tag`; plugins document themselves.
- **Doors:** the `oaken` CLI, a local HTTP API, an MCP server, and a console (loopback only) that draws every plugin's settings.
- **Models:** a router with local, LAN and hosted backends, an answer cache and a ledger; confidential items stay local.
- **Products:** `identity.configure(app, title, prog, daemon)` lets a package wrap core under its own name and home.
