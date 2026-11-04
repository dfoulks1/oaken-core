# 12. Plugin extension hooks: job kinds, pipeline stages and scheduled tasks

- Status: accepted
- Date: 2026-10-22

## Context

A plugin could already contribute the five kinds (provider, embedder, LLM backend, enricher,
vector store) and, since the operation registry, named operations. The Stage-1 audit
(`docs/prompts/CORE_34-stabilize-core.md` §3) found three gaps a second developer would hit: a plugin
could **not** add a background job kind, a pipeline stage, or a scheduled task without editing
core. Each was a small, additive hook away.

## Decision

Three optional methods on any plugin, collected by the host from the healthy plugins and wired into
core at start-up. All three give the plugin a **plugin-scoped context with no database handle** —
the same boundary as a plugin operation — and are validated at load like operations, so a malformed
declaration is a plugin problem (logged, the plugin unhealthy) and never a core crash.

- **`job_handlers() -> list[PluginJobHandler]`.** Each `(kind, payload_model, handler,
  concurrency?, result_model?)` is registered on the durable job runner as **`plugin.<name>.<kind>`**
  — namespaced so two plugins cannot collide and a plugin kind is always distinct from a core one.
  The handler is given a `PluginJobContext` and its validated payload; a result is validated against
  `result_model` and stored, exactly as for a core kind. A plugin failure is reduced to its
  exception type (it could quote mail). Registered in the `jobs` service unit after the host starts.

- **`pipeline_stages() -> list[PluginStage]`.** Each `(name, handler, version)` runs **after the
  enrichers** for a message. The handler gets a `PluginStageContext` and a `PluginStageInput` — the
  message plus its stored enrichment results — and returns JSON data core stores under
  **`plugin.<name>.<stage>`** with the **same content-hash idempotency as an enricher**, so a rerun
  or a `reprocess` is a cache hit, never recomputed. A stage failure is logged and never fails the
  message's enrich job. Stages reach the pipeline through `Backends` (callables, like enrichers), so
  `oaken.ingest` does not depend on the plugin host directly.

- **`scheduled_tasks() -> list[PluginScheduledTask]`.** Each `(name, job_kind, payload, every|cron)`
  is seeded into the scheduler as **`plugin.<name>.<task>`** **only if absent** — a schedule the user
  has since edited or removed is left alone, the same rule as the per-account sync schedules. The
  task fires its `job_kind` (commonly the plugin's own `plugin.<name>.<kind>`) with a fixed payload.
  Seeded in the `jobs` service unit after the host starts, before the scheduler starts.

### Why these shapes

- **Namespacing (`plugin.<name>.<...>`)** keeps a plugin's kinds, stages and tasks collision-free
  and visibly distinct from core's, and lets the audit log and the queue name them unambiguously.
- **No database handle.** A plugin returns data and core stores it (ADR 0002's rule for providers),
  so a hook cannot corrupt a store or bypass the audit log.
- **Idempotency reuse.** A pipeline stage reuses the enrichment store's `(content_hash, name,
  version, model)` key, inheriting the pipeline's crash-safety and reprocess semantics for free,
  rather than inventing a parallel cache.
- **Seed-if-absent.** A scheduled task respects a user's later edit, matching the established
  sync-schedule rule, so a plugin update cannot silently reset a schedule the user changed.

These additions are **additive within `PLUGIN_API_VERSION` 1** (ADR 0010): new optional methods and
new contract dataclasses, pinned in the surface snapshot. No existing plugin changes behaviour, and
the host treats a plugin that defines none of them exactly as before.

## Consequences

- A second developer adds a background job, a post-enrich derivation, or recurring work by writing a
  plugin method — no edit to `oaken.daemon.core`, the pipeline, or the scheduler.
- Each hook has a conformance suite in `oaken.testing.contracts`
  (`JobHandlerContract`, `PipelineStageContract`, `ScheduledTaskContract`, and `verify_*` forms) so a
  plugin author checks their declarations the way the host will.
- `docs/plugins.md` gains worked "how to extend" examples for each, and the generated
  `reference/plugin-contracts` page lists the new types automatically (it parses `contracts.py`).
- The hooks are intentionally small. A richer scheduling API (per-task enable/disable, a plugin
  enqueuing its own kind with helpers) can come later without changing these signatures.
