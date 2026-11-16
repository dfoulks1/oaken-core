# Changelog

## Unreleased

- **Agents:** an agent now has a `locality` (`local`, `lan` or `hosted`), stated by the person in `agent_create` and shown by `agent_list`.
  It defaults to `hosted`, the strictest, and existing agents become `hosted`. Only a `local` agent may be handed confidential content. New
  `oaken_core.confidential` holds the one rule for what is confidential and who may see it (migration 0009).
- **Swarm:** `job_claim` no longer hands a confidential job to an agent that is not `local`. The claim skips, in its own transaction, a job
  whose subject's effective `confidential` label is `true` or whose payload says it is confidential; the job waits for a local agent or
  core's own workers. Before, any agent allowed a kind received every job of it, confidential or not.
- **Router:** `locality = "local"` is no longer taken on the owner's word alone. An LLM backend or embedder may say, once started, that it sends
  text to another machine (`off_machine = True`, new on `BaseLLMBackend` and `BaseEmbedder`); the router then treats it as `hosted` and logs an
  error, so a confidential item cannot reach another box through a mistyped locality. A plugin that does not say keeps the owner's word.

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
