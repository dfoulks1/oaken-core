# Changelog

## Unreleased

- **Agents:** an agent now has a `locality` (`local`, `lan` or `hosted`), stated by the person in `agent_create` and shown by `agent_list`.
  It defaults to `hosted`, the strictest, and existing agents become `hosted`. Only a `local` agent may be handed confidential content. New
  `oaken_core.confidential` holds the one rule for what is confidential and who may see it (migration 0009).
- **Router:** `locality = "local"` is no longer taken on the owner's word alone. An LLM backend or embedder may say, once started, that it sends
  text to another machine (`off_machine = True`, new on `BaseLLMBackend` and `BaseEmbedder`); the router then treats it as `hosted` and logs an
  error, so a confidential item cannot reach another box through a mistyped locality. A plugin that does not say keeps the owner's word.
- **MCP:** a confidential gate on both MCP doors. A caller now carries a clearance for confidential items, set by its door: the person's
  `/mcp` door only with the new `[api] mcp_confidential = true` (default `false`), the agent door only for a `local` agent. An uncleared
  client gets `confidential_withheld` from `item_show`, `item_get`, `label_get` and a `label_set` proposal on a confidential item, and
  `label_find` and `log_list` leave such items out. Plugin commands reach an uncleared client only if they declare
  `@command(..., confidential_aware=True)` and honour the new `PluginOpContext.confidential`. **Behaviour change:** an MCP client that read confidential items before needs the setting (or a
  `local` agent), and plugin commands that do not declare `confidential_aware` drop out of an uncleared client's tool list.

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
