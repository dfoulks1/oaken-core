# oaken-core plan

AI-generated draft, unreviewed. Core is an async pipeline and a plugin loader. It has no nouns:
no message, no person. Products (oaken-mail, oaken-grove) are plugins that import `oaken_core`.

## Done (step 0)
Lifted from oaken-mail `cf3b187` (last commit, not the dirty tree), imports renamed to `oaken_core`,
106 lifted tests pass on Python 3.12, every module imports:
`clock ids* errors actors paths cron db untrusted config_edit settings_edit logging*`,
`operations/{registry,gate,caller,openapi}`, `api/{guard,ratelimit,runtime,errors,models}`,
`plugins/authorize`. (*ids: mail ids replaced by `SubjectId`. logging: tiny new `settings.py`.)
Three tests wait in `tests_to_port/` (registry, gate, guard). They need a new test rig.
ADRs 0003, 0012, 0016 to 0019 are in `docs/origin/` for reference.

## Decided
- Core = async pipeline + plugin loader + operation registry + kind contracts + labels + change log.
- Labels and the change log live in core, keyed to a generic `SubjectId`.
- Queues: one `default` by default; more via `oaken_core.new_queue(name, description, ...)`.
  Jobs do not pick a queue. A triager labels the item first; reserved label `queue` routes it.
- Intake is the one queue triagers run in. Triage runs BEFORE ingestion, so junk is never stored.
  A failing triager lets intake back up (no fallback queue). Core never auto-purges intake.
- `queue.requeue` (job or selector, `--to`, `--retriage`), `mcp="never"` by default, a config
  setting may lift it. Built from oaken-mail's `retry_job`.
- Core ships the form mechanism (`UiSpec`, checker, default modal) and a baked web renderer
  (the console). The console is loopback only, never off, no login. Auth (`BaseAuth`) guards the
  API/MCP/gateway endpoint, where `remote_ok` lives. Core serves `UiSpec` as JSON plus a minimal
  Jinja fallback.
- No oaken dependency copies: products depend on `oaken-core` as a package.

## Defaults awaiting a yes (treated as yes until told otherwise)
Model router in core, backends as plugins. Rejects keep a content-free log row only.
`confidential` settable at intake. Scheduled jobs name their queue directly. Locks and pending
changes live in core as a generic `(subject, field)` mark.

## Build order
1. db + one ordered migration runner (core and plugin tables); change log; labels
   (`BaseLabel`, `ReservedLabel`, `LabelMaker`, `LabelError`).
2. jobs with `subject` (not `account_id`), named queues, label routing, intake, requeue, scheduler.
3. plugin loader written new: kinds, entry points, `config_model`, `schema`, hooks, health,
   `requires_core` range, acyclic `needs` check.
4. skins: generic services/client, HTTP (starlette), CLI flag generation, MCP bridge.
5. router + backend kind + cache; `confidential` enforcement.
6. console: `UiSpec`, checker, default modal, baked loopback renderer, Host/Origin/CSRF guard.
7. conformance kit: a stub plugin per kind; port `tests_to_port/`.

## Not lifted, on purpose
oaken-mail `jobs`, `events`, `services`, `scope`, plugin `host/base/contracts` (mail-coupled by
`account_id`, mail events, per-account instances). They are written new, using the originals as a guide.

## Risks
`oaken-core` is also the name of oaken-mail's bundle package (owner will sort the name).
Intake holds raw unstored payloads in queue rows. Unverified: PyPI name; contracts and the Jinja
templates were not read for mail copy; nothing is committed.
