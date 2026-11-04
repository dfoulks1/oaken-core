# Step 5: the model router

Goal: analyzers ask for a model. Core finds one. Remote and local both work.

Lift from oaken-mail: `LLMRequest`, `LLMResponse`, the `LLMBackend` contract, the answer cache
(`ai/cache.py`), `ai/generate.py` retry logic. Read `src/oaken_ollama/` as the model backend plugin.
Drop workspaces. Drop the single global model gate.

Build:
1. Backends are named config entries: plugin, URL, `locality` (local, lan, hosted), `concurrency`.
   Any number. Each is a named instance from step 3.
2. `resolve(analyzer, call) -> backend`. An analyzer names a backend and a model. An optional ordered
   `fallback` list is tried when the first is unhealthy.
3. Each backend has its own semaphore. One slow box does not stall the rest.
4. `confidential` routes to `local` only. A LAN backend works if the owner allowlists it.
   The router refuses. It never falls back to a remote box.
5. `BaseAnalyzer.ask`: one call through the router and the cache. Validate against `output_model`.
   A bad answer fails the job and stores nothing. The cache key is
   `(analyzer, version, content hash, effective model)`.
6. Ship a stub backend for tests. Hosted and OpenAI-compatible backends live in a plugins repo.

Done when:
- Two backends serve one model. A call goes to the named one. It falls back when that one is down.
- A `confidential` call never reaches a `lan` or `hosted` backend.
- Changing an analyzer's model changes its cache key.
