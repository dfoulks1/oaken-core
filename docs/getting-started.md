+++
title = "Set up Oaken Core from scratch"
subtitle = "From install to a running core and its console"
lead = "Install core, start it, look at what it runs, add a plugin. Core alone has no product: a first run shows the built-in plugins and an empty pipeline. Each step says how to check it worked."
badges = [["text-bg-warning", "Alpha"]]
group = "start"
order = 10
description = "A first-time setup of Oaken Core: install, set OAKEN_HOME, oaken start, oaken plugins, oaken plugin add KIND.NAME, and the console on 127.0.0.1:62536."
+++

Everything below stays on your machine.

## 1. Install

You need Python 3.11 or newer.

```bash
python3 -m pip install oaken-core   # into a virtual environment you keep for it
oaken --help
```

To work on core itself, use a checkout: `uv sync`, then `uv run oaken ...`.

`oaken --help` works with no core running. It lists core's own commands. A plugin's commands
appear only while core runs.

## 2. Pick a home

Set `OAKEN_HOME=/some/dir` to put everything under one directory, and put the line in your shell
profile so every shell agrees.

| Path | Holds |
| --- | --- |
| `config/` | `config.toml` (when you or a command write one), `api_token`, `mcp_token` |
| `data/` | `core.db`, `vectors.db`, `events.jsonl`, plugin databases |
| `plugins/` | folders plugins own |
| `run/` | `runtime.json`: pid, API port, console port |

Every folder is created owner-only (mode 0700). Core does not write a `config.toml` on first
start. Without one it runs on defaults.

If `OAKEN_HOME` is unset, core uses the home it last ran under (a pointer file in your user
state folder, written only when the variable was set), or else the platform folders. Core cannot
export a variable into your shell.

## 3. Start core

```bash
oaken start      # in the background; --fg keeps it attached to this terminal
oaken status     # version, queues, plugins healthy
```

`start` prints `core running (pid ..., port ...)`. `status` exits 3 with `core is not running` when
there is no core. If `start` fails ("core exited while starting"), run `oaken start --fg`. It
prints the real error, such as a port in use.

Core serves its API on loopback only. [The daemon](architecture/daemon.md) explains the rest.

A first run shows three queues (`default`, `store`, `embed`), all empty, and nine built-in plugins,
seven of them healthy. Nothing flows yet: no provider brings items in.

## 4. Look at the plugins

```bash
oaken plugin list    # what core runs, and why one did not start
oaken plugins        # what you can add
```

Two plugins do not start on a fresh core, and `plugin list` says why:

- `provider.socket` needs an instance that sets `unix_path` or `port`.
- `triager.llm` needs `criteria` and a model backend plugin.

`oaken plugins` lists plugins you can add by name, from `[extensions] dirs` in `config.toml`
first, then from the `oaken-plugins` package. With neither, it prints
`Nothing to list. Set [extensions] dirs in config.toml, or: pip install oaken-plugins`. It needs no
running core. Filter it with `--kind provider` or `--tag mail`. See [Plugins](plugins.md).

## 5. Add a plugin

```bash
pip install oaken-plugins
oaken plugins --kind provider
oaken plugin add KIND.NAME           # e.g. provider.socket
oaken plugin list                    # the new row should say healthy
```

`plugin add` writes a `[plugins.<kind>.<name>]` table to `config.toml`, starts the plugin, and
reloads core if it had to find it. Without `--yes` it shows what would change and exits 5.
`--name` gives one instance its own name, so you can run two of a plugin. `--config JSON` sets its
settings. A secret in there is refused: [Store credentials](keyring.md).

A plugin that fails to start is listed with the reason, not hidden. `oaken plugin disable NAME`,
`enable` and `reload` act on one without restarting core.

## 6. Open the console

The console is at `http://127.0.0.1:62536/`. It always binds 127.0.0.1. Change the port with
`[console] port` (0 picks a free one). It has pages for the pipeline, each stage, each plugin, the
log, capabilities and `/config`, where you edit `config.toml` as text: it checks a change before
saving and keeps one backup. See [Settings, storage and the event log](settings.md).

## 7. Stop

```bash
oaken stop
```

## If something fails

| Message | Likely cause |
| --- | --- |
| `core is not running` | Start it with `oaken start`. |
| `core is already running` | A daemon is up. `oaken status` shows it; `oaken stop` ends it. |
| `core exited while starting` | Run `oaken start --fg` for the error. |
| `the OS keyring failed: ...` | See [the keyring page](keyring.md). |
