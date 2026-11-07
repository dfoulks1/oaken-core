# Changelog

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
