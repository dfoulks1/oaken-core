# 17. Triagers and enrichers are separate kinds

- Status: accepted (ratified 2026-11-03 by the owner, who verified the built implementation for 0.2.0)
- Date: 2026-11-03
- Amends: ADR 0012 (the `Triager` and `Enricher` protocols stop sharing types) and ADR 0010 (the
  contract snapshot changes; `PLUGIN_API_VERSION` is already 2 for 0.2.0, against 1 in 0.1.0, and the
  rename of `EnrichInput` rides on that bump)
- Built: 2026-11-03, not yet verified by the owner's checks: decisions 1, 2, 2c, 3, 4 and 5, the
  `oaken_triagers` package, `triage_verdicts` (migration 0019), `BaseAnalyzer` and `core/triage.py`.
  Built later the same day: 2b (one result-store helper, `_cached_result`/`_store_result`), 2d (the lazy
  chain: `ingest.triage` runs the first triager, `ingest.triage.<name>` the next), the job-kind names
  in `core/triage.py`, one `MessageTriaged` event, the `llm.triage` stage and the `triager` trace field,
  and the swarm wire's `triager` entry. Not built: the validators still share `to_result`. Prompt: `docs/prompts/CORE_55-separate-triagers-and-enrichers.md`.

## Context

A triager rates a message so core can decide what else it is owed. An enricher derives something
from a message. They were built as one idea, and the code still says so. The owner's ruling:
"triagers are not enrichers and shouldn't be enrichers at any level", and "we need to put any
definitions they share in BasePlugin and then detangle the two concepts." The owner calls this a
small re-architecture, the first in oaken-mail.

Premises, each checked on 2026-11-03:

- `BaseEnricher` and `BaseTriager` repeat `version`, `needs_llm`, `output_model`, `ask_intent` and
  `output_schema` (`src/oaken/plugins/base.py`).
- A triager's signature is `triage(msg: EnrichInput) -> EnrichOutput | None`, and the `Triager`
  protocol uses the same two types (`src/oaken/plugins/contracts.py`).
- `LLMTriager` takes `EnricherConfig` as its config model and mirrors `LLMEnricher`
  (`src/oaken_enrichers/_common.py`).
- `spamcheck`, `rules` and `ai` live in `oaken_enrichers` (`pyproject.toml` entry points).
- The verdict is stored as an enrichment named `triage` (`GATE`, `src/oaken/ingest/pipeline.py`).
  `enrich_message` also runs triage; the usage stage is `llm.enrich.triage`.
- `v0.1.0` has no `BaseTriager` and `PLUGIN_API_VERSION` was 1: triagers are new in 0.2.0, so no
  out-of-tree triager exists. Out-of-tree *enrichers* do exist, and the rename of `EnrichInput` to
  `CleanMessage` breaks them: it is a breaking change, carried by the API version bump and the 0.2.0
  number (ADR 0010), and the release notes give the one-line fix (change the import).

## Decision

1. **A `BaseAnalyzer` holds what the kinds that read mail share.** It is an intermediate helper
   class between `BasePlugin` and `BaseEnricher` / `BaseTriager` (and `BaseAction`, ADR 0019): `version`, `needs_llm`,
   `output_model`, `output_schema`, the check of a result against `output_model` and
   the model handle (`ModelHandle`). It is not a plugin kind: no entry-point group, no operation, no
   CLI command, and nothing outside the plugin classes names it. `BasePlugin` keeps only what every
   kind has, so a provider, an embedder or a cache no longer inherits `needs_llm`. The verbs stay
   separate (`enrich`, `triage`); the analyzer has no abstract method.
   *Cited:* "analyzer isn't a thing you can call from the CLI, it's an intermediate helper class."
2. **One neutral input, two outputs.** `EnrichInput` is renamed `CleanMessage`; both kinds read it.
   A triager returns a `TriageVerdict` of its own, not an `EnrichOutput`. The `Triager` protocol
   no longer mentions an enricher type. No aliases for the old names.
2a. **Triage is a core concept, `oaken.core.triage`.** The verdict's shape (`TriageResult`, the
   categories), the levels (the number-and-name labels, the built-in 0 to 3, the urgency a level
   gives a job), the `triage` stage and job-kind names and `triager_for(kind)`, the usage stage
   `llm.triage` and the `MessageTriaged` event name are defined once there. Core imports no plugin;
   `TriageVerdict` validates against core's shape; `BaseTriager` builds on core's levels (ADR 0016).
   *Cited:* "or is it just the concept of triage in core?"
2b. **The mechanism after a plugin answers is written once.** One validator (not `to_enrich_output`
   and `to_triage_verdict`), one result-store helper in core parameterized by table (cache key, the
   look for an existing or same-content result, the upsert, the trace line, the usage record), used by
   both `enrichments` and `triage_verdicts`. The two stage loops stay separate: run order, first
   verdict wins and gating differ.
   *Cited:* "triage is an extremely-similar-but-not-exactly-the-same copy of enrich."
2c. **`ask` is removed.** The question-answering operation, its intent router and everything that
   served it go: the `ask` operation, `core/search/ask.py`, `route.py` and `quotes.py`, `AskIntent` and
   `ask_intent` on every plugin base, `ask_sources()`, the `oaken ask` command and the ask model
   setting. A bigger model reads the mail through `search`, `get_message`, `get_enrichment`,
   `get_triage` and `get_action` over MCP. If `ask` returns, it returns as an optional action plugin that queues
   enrichment jobs, configured and switched on only by a reader who wants it.
   *Cited:* "lets drop ask entirely. if we really want to make ask a thing later we can make it an
   action plugin that submits get_enrichment jobs to the queue"; "then you can configure it how you
   want (if you want it)."
2d. **Triage is a lazy chain of per-triager jobs.** Each triager runs as its own atomic job, in `rank`
   order, and the next triager's job is queued only when the one before it returns no verdict. The first
   verdict wins and ends the chain, so a model is asked only for mail the cheaper triagers leave. Each
   job has its own retries and concurrency, and a swarm agent can claim one. Enrichers and actions fan
   out instead, one job per plugin and per route. The job-kind names are open (`ingest.triage.<name>` is
   the drafter's suggestion).
   *Cited:* "each kind of analyzer run is run as an atomic job ... gets its own agent"; "lazy loaded
   analysis, lol."
3. **The triagers get their own package, `oaken_triagers`.** `spamcheck`, `rules` and `ai` move
   there with their tests; the entry-point names stay, so `[plugins.triager.*]` config is
   unchanged. `oaken_enrichers` holds enrichers only. The shared LLM-access helper moves to the
   plugin layer, neutral; the import-linter roots and docs follow.
4. **The verdict has a store of its own.** A `triage_verdicts` table (migration `0019`) holds it.
   The migration moves each existing `triage` enrichment row into it and deletes the old row; no
   model is called. The pipeline gate, `min_priority`, search and every other reader are re-pointed.
5. **Operations follow.** `get_triage` returns the verdict and `get_enrichment` returns enrichers
   only. `enrich_message` runs enrichers only; triage runs through `triage_message`, and
   `try_triage` stays. The usage stage becomes `llm.triage`. One conformance suite per kind.
6. **`ai` moves as it is.** Its role is not ruled yet; no behaviour changes.
7. **This is built before ADR 0016,** which rests on it, and 0.2.0 waits for both.

*Cited:* the owner's words above; Q1 to Q8 and Q10 of `docs/bbq/2026-11-03-adr-triager-enricher-split.md`.

## Alternatives

- **Keep the verdict an enrichment and separate only the classes.** Smaller. Overruled by the
  owner: a verdict stored as an enrichment is still a triager that is an enricher.
- **Separate input types (`TriageInput` and `EnrichInput`).** Nothing shared, every message-shape
  change made twice. Overruled by the owner in favour of the neutral `CleanMessage`.
- **Stay in `oaken_enrichers`.** Overruled: the package name would keep saying they are enrichers.
- **Fold ADR 0016 in.** One review, but a contract split mixed with new capabilities. Overruled.

## Consequences

- The plugin contract and operation snapshots change; `PLUGIN_API_VERSION` stays 2 (unreleased).
- Stored data changes: migration `0019` rewrites rows in the owner's live `~/.oaken` database. Back
  it up first. A failed migration must leave the old rows in place.
- `get_enrichment` stops returning the verdict, which changes the CLI, MCP and console readers.
- Docs, the CLI reference and the operations reference regenerate.

**Confirmed by:** the contract and operation snapshot tests, a migration test over a database
holding old `triage` rows, and a read of `docs/plugins.md` by the owner.

**Revisit when:** the owner rules on the `ai` triager's role, or a third kind of analyzer (an action) is built.

## History

- 2026-11-03: drafted from the owner's answers in a working session. Not ratified. Only the owner
  ratifies.
- 2026-11-03: ratified by the owner, who verified the built implementation works for 0.2.0.
- 2026-11-03 (after the first build, before ratification): the owner ruled decision 1 goes to a
  `BaseAnalyzer` (an internal helper, not callable) and not `BasePlugin`; added decisions 2a (triage
  is a core concept) and 2b (the mechanism once). The first draft and the first build put the shared
  members on `BasePlugin`, and copied the enrichment store for the verdict. Open: kinds `enrich`,
  `triage` and `action`, a plugin registering as one or more, and separate attachment queues.
- 2026-11-03 (same session): the owner ruled the release is 0.2.0, because of the base type split. The
  premise that nothing shipped is corrected: out-of-tree enrichers exist and the rename breaks them.
- 2026-11-03 (same session, before ratification): the owner ruled each analyzer run is an atomic job and triage
  is lazily loaded. Decision 2d added. The first draft ran the whole triage chain inside one job.
- 2026-11-03 (same session): the owner ruled `ask` is dropped entirely. Decision 2c now removes it, and
  `ask_intent` leaves `BaseAnalyzer`. The earlier 2c had `ask` read the analyzers directly.
