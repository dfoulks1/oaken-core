+++
title = "What 1.0 promises"
subtitle = "What will not break inside 1.x, and what may"
lead = "1.0 is a promise about a short list of surfaces. Anything on the list keeps working across every 1.x release. Anything off it can change in a minor release, and the changelog will say so."
group = "project"
order = 11
description = "The compatibility contract of oaken-core 1.x: the plugin API, operations, CLI exit codes, HTTP paths, config keys, database migrations and plugin module paths that are frozen, and what is not."
+++

Core and `oaken-plugins` follow semantic versioning. A change that breaks something
on the frozen list waits for 2.0. A new operation, setting, kind or optional field is not a break and may
arrive in any 1.x.

## Frozen in 1.x

| Surface | What is frozen |
|---|---|
| **Plugin API** | `PLUGIN_API_VERSION = 1`. The members each kind requires (`oaken_core.plugins.kinds`), the lifecycle (`start`, `stop`, `health`), the contract types in `oaken_core.plugins.contracts`, `Verdict` and the intake envelope fields, and the `requires_core` range check. A plugin written for API 1 loads on every 1.x. |
| **Entry-point groups** | The group names (`oaken_core.triagers`, `oaken_core.llm_backends`, ...) and the kind names. |
| **Operations** | The name, the permission class, the input fields and the output fields of every built-in operation. New optional inputs and new output fields may appear. |
| **CLI** | Command names, flag names and the exit codes 0 to 5 (see [Operations](architecture/operations.md#cli-exit-codes)). Human-readable text is not frozen: use `--json`. |
| **HTTP API** | The paths under `/v1/` (`health`, `operations`, `operations/{name}`, `openapi.json`, `events`, `trace/stream`, `agent/jobs/*`), the error `code` values and the status each maps to, and the `/mcp` and `/mcp/agent` doors. |
| **Configuration** | Every documented key of `config.toml`, with its meaning and default. A key may gain values. A key is deprecated for a whole minor series before it is removed in 2.0. |
| **Environment** | `OAKEN_HOME`, `OAKEN_URL`, `OAKEN_TOKEN_FILE`, `OAKEN_STATE_DIR`. |
| **Data** | Migrations only add. A 1.x core opens the database any earlier 1.x wrote and migrates it forward. There is no downgrade. |
| **Confidentiality** | The label `confidential`, the three gates (router locality, MCP clearance, agent locality) and the rule that a gate refuses when in doubt. A release may tighten a gate and never loosen one. |
| **`oaken-plugins` names** | The distribution name, the module path `oaken_plugins.<kind>_<name>`, each plugin's class name and its `[plugins.<kind>.<name>]` settings keys. A plugin that is retired stays importable for the whole 1.x series. |

## Not frozen

- **The Python import API.** `import oaken_core` is how the tests and the plugin contract reach core, not a
  library you embed against yet. Only what is under *Plugin API* above is stable. Everything else
  (`oaken_core.app`, `oaken_core.jobs`, ...) may change in a minor release.
- **Output text**: log lines, error messages, console pages, the generated docs.
- **Internals**: table names, queue and job internals, trace event shapes, the console's HTML.
- **Defaults of tuning values**: timeouts, limits, leases, rate limits. They may change when measured.
- **Plugin catalog contents.** A new plugin may appear in any `oaken-plugins` 1.x. A plugin's behaviour may
  improve. Its settings keys do not change meaning.
- **Optional extras** (`oaken-plugins[imap]` and the rest): the libraries behind them move with their own
  releases.

## How a break is handled

1. **Deprecate first.** The old form keeps working and logs a warning that names its replacement.
2. **Say so.** The [changelog](https://github.com/dfoulks1/oaken-core/blob/main/CHANGELOG.md) has a *Deprecated* entry the release that starts it.
3. **Remove in the next major.** Never in a minor.

A security fix is the one exception. If a frozen behaviour is the hole, it changes in a patch release and the
changelog and the advisory say what and why.

## Versions of core and plugins together

`oaken-plugins` 1.x depends on `oaken-core>=1.0.0,<2`. A plugin may need a newer 1.x core and says so with
`requires_core`; `oaken plugin add` refuses it with that reason. See [Releasing](releasing.md) for the pin and
[Using plugins](using-plugins.md#versions-and-compatibility) for what the user sees.
