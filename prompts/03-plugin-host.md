# Step 3: the plugin host

Goal: core loads plugins by kind, store first, and runs them.

Lift from oaken-mail:
- `plugins/host.py` (1,050 lines). Light surgery.
  Generalize `start_account_instance` into named instances: `<plugin>@<id>`. The ids are config
  entries, not accounts. Remove the rest of the account code.
- `plugins/contracts.py`. Keep the generic half: `PluginContext`, `Health`, `PluginOperation`,
  `UiSpec` and friends, `PluginJobHandler`, `PluginScheduledTask`, `Plugin`, LLM, cache.
  Rewrite what is mail-typed: `Provider`, `RawMessage`, `CleanMessage`, `PluginStageInput`.
- `plugins/base.py`. Keep `BasePlugin`, `BaseLLMBackend`, `BaseAnalyzer`. Adapt the rest.
- `plugins/validate.py`, `plugins/adapt.py`, `plugins/jobs.py`.

Build:
1. `register_kind()`. The eight shipped kinds register through it. A product can add its own.
   The kind list is not a closed `Literal`. Entry-point groups are `oaken_core.<kind>s`.
2. Load order: settings, store, then everything else. The store is a plugin. Core needs exactly one.
3. `requires_core`: a version range on each plugin. Refuse a plugin outside it.
4. `needs` between analyzers must be acyclic. Check at load. A cycle disables the plugin that closes it.
5. A bad label write from a plugin raises `LabelError`. The plugin is disabled. The host carries on.
6. One migration runner across core and every plugin's tables, in one order.
7. An analyzer's input is an opaque view the product builds. Core assumes no text. The view supplies
   its own content hash. Relations are `self`, `parent`, `children`, plus ones a product registers.

8. The host feeds the operation registry. A plugin's `operations()` (and its command groups) are
   registered as the plugin loads, so step 4's CLI can build itself from the registry afterward.
   A plugin that fails to load drops its operations. Nothing else breaks.
9. `needs_llm` job kinds are added to the agent-eligible kinds (`SwarmSettings.allowed_kinds`
   still decides; the owner opts in).

Done when:
- A stub plugin of every kind loads, starts, reports health, stops.
- A second named instance of one plugin runs beside the first.
- A plugin built for the wrong core version is refused.
- A cycle in `needs` disables the plugin that closes it.
