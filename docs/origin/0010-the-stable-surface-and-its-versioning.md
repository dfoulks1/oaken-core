# 10. The stable surface, its versioning and how it is protected

- Status: accepted
- Date: 2026-10-22

## Context

Core should rarely need to change once we know what it must do, so a second developer can build
features — providers, enrichers, operations and, later, job kinds and pipeline stages — without
editing the core package. That only holds if the **public surface** a plugin, a client or an AI
caller depends on is named, versioned and protected against accidental change.

An audit (`docs/prompts/CORE_34-stabilize-core.md`, Stage 1) found the surface mostly sound but
**unprotected in shape**: a field renamed on a plugin contract dataclass, a retyped operation
output, a bumped `PLUGIN_API_VERSION` or a changed streamed event all break out-of-tree code with
no failing test. Operation *routes* and the OpenAPI document are generated from the registry (so
they cannot drift from it), but the rest of the contract is not pinned.

## Decision

### The public surface (what a second party may depend on)

1. **Plugin contracts** — `oaken.plugins.contracts` and the base classes in `oaken.plugins.base`:
   `PLUGIN_API_VERSION`, `PLUGIN_KINDS`, `ENTRY_POINT_GROUPS`, `CAPABILITIES`, `REQUIRED_MEMBERS`,
   the five `Protocol`s, and the frozen dataclasses that cross the boundary (`RawMessage`,
   `PullBatch`, `FlagChange`, `EnrichInput`, `EnrichAttachment`, `EnrichOutput`, `VectorItem`,
   `Hit`, `VectorStats`, `PluginOperation`, `PluginContext`, `PluginOpContext`, `PluginChange`,
   `PluginPreview`, `Health`, `PluginInfo`).
2. **The operation registry** — each operation's `name`, `permission`, `mcp` ceiling,
   `marks_read`, `streaming`, and its input/output JSON Schemas. The HTTP and MCP surfaces are
   generated from these, so pinning them pins those too.
3. **The event stream shapes** — the field names of every `Event` subclass that can reach a client
   stream (the SSE endpoint, an MCP client, an in-process consumer).
4. **The `[plugins.<name>]` config contract** — free-form by design; a plugin validates its own
   table (see ADR on plugin config validation / `config_model`).

### The versioning and deprecation rule

- The public surface changes **additively** within a `PLUGIN_API_VERSION`. A new optional field on
  a contract dataclass, a new operation, a new event type, a new optional operation input with a
  default — all additive, no version bump.
- **Removing or renaming** a field, changing a type incompatibly, tightening a permission, or
  removing an operation or event is a **breaking change**: it bumps `PLUGIN_API_VERSION` and is
  called out in the changelog/release notes. The host already refuses a plugin whose
  `api_version` does not equal core's, so a bump is a clean, visible break rather than a silent one.
- A decision that is expensive to reverse gets its own ADR; this ADR is the standing rule.

### How it is protected: snapshot tests

Each public seam has a **snapshot test** that compares the live surface against a committed JSON
baseline and fails on any unreviewed change:

- `src/oaken/core/operations/tests/test_schema_snapshot.py` ↔ `operation_schemas.json`
- `src/oaken/plugins/tests/test_contract_snapshot.py` ↔ `contract_snapshot.json`
- `src/oaken/core/tests/test_event_snapshot.py` ↔ `event_snapshot.json`

A snapshot failure is a prompt to a human: either the change is intentional (regenerate the
baseline with the documented `--update` switch, and the diff is reviewed in the pull request as the
record of a surface change) or it is accidental (revert it). The baselines are small and cover only
the *stable* surface, so they do not fire on internal refactors.

### Two boundaries recorded here

- **`Settings` stays `extra="forbid"`.** A plugin or subsystem does **not** get to add a typed
  top-level config section dynamically. Rejecting an unknown key is a safety property — it catches a
  typo in a file that controls someone's mail. A new **core** section is a deliberate core change
  with (if weighty) its own ADR. A *plugin* validates only its own `[plugins.<name>]` table, via
  `config_model`, and that validation is additive (a plugin without one keeps the free-form dict).
- **`oaken.api.runtime` is a sanctioned client seam.** A client may read the runtime
  file to find and check the running core (`read_runtime`, `pid_alive`, path helpers), alongside
  `oaken.__version__`, `oaken.core.paths` and `oaken.core.operations.caller.CallerContext`
  (ADR 0006). The import-boundary contracts encode exactly this allow-list. This slightly widens
  ADR 0007's "version and path helpers" wording, on purpose: finding core is a path/runtime concern,
  not reaching into its internals.

## Consequences

- A contributor who renames a contract field or an operation output sees exactly one snapshot test
  fail, with a readable diff, and must either bump `PLUGIN_API_VERSION` (breaking) or make the
  change additive. The surface can no longer drift silently.
- The snapshots are checked in CI with everything else, so a pull request that moves the surface
  shows the moved surface in its diff — the review record the ICLA and the EU AI Act labelling rule
  both want for public-facing change.
- Updating a baseline is a deliberate, documented act, not something a formatter or an unrelated
  refactor does by accident.
- This ADR does not freeze the surface; it makes moving it visible and versioned.
