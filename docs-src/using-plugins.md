+++
title = "Using plugins"
subtitle = "Find, add, configure and manage the plugins in oaken-plugins"
lead = "Core is the pipeline and the plugin host. What an item is, where it comes from and what happens to it are plugins. This page is how you use them: how they are packaged, how you add one, how it is configured, and what to do when one does not start."
group = "start"
order = 12
description = "How to use Oaken plugins: the oaken-plugins package, oaken plugins and plugin add, config.toml, templates and instances, extras, secrets, extension folders and version checks."
+++

## Two packages, one catalog

| Package | What it holds |
|---|---|
| `oaken-core` | The pipeline, the plugin host, the operations, the CLI, the API, the MCP door and the console. It also carries a few built-in plugins, so intake works with nothing else installed (`socket`, `dedupe`, `rules`, `llm`, `items`). |
| `oaken-plugins` | The plugins you add for your own use: providers, formatters, triagers, enrichers, ingestors, model backends and more. One distribution, with a subpackage per plugin. |

```bash
pip install oaken-core oaken-plugins
```

Installing `oaken-plugins` changes nothing in a running core. It puts the plugin code on disk, and no plugin is
imported or switched on until you add it. It declares no entry points, so there is nothing to uninstall to turn a
plugin off, and nothing loads by accident.

Why a second package? Core is the stable foundation, and its surface is versioned and frozen
([ADR 0010](repo:docs-src/origin/0010-the-stable-surface-and-its-versioning.md)). Plugins change more often, one at a time, and
some need heavy libraries. Keeping them out of core means core stays small and releases slowly, and a plugin fix does not
touch the foundation. `oaken-plugins` needs core (`oaken-core>=1.0.0,<2`); core does not need it.

Plugins are **not** add-ons in the pip sense: there is no `oaken-plugins[imap-provider]`. An extra can only pull in
a library, not switch a plugin on (see [Extras](#extras)). Adding a plugin is `oaken plugin add`.

## Find a plugin

```bash
oaken plugins                           # everything in oaken-plugins you can add, with a one-line summary
oaken plugins --kind provider --tag mail  # narrow it by kind and by what it is for
oaken plugin describe formatter.ini     # the plugin's own documentation
```

`oaken plugins` reads the installed package without importing any plugin. Each plugin is named `KIND.NAME`, for example
`formatter.ini` or `triager.spamfilter`. Folders listed in `[extensions] dirs` appear first
(see [Your own plugins](#your-own-plugins)).

`plugin describe` is the plugin's own page: what it is, how to set it up, its settings with types and defaults, its
commands, and an example for `config.toml`.

## Add a plugin

```bash
oaken plugin add formatter.ini          # shows what it would change
oaken plugin add formatter.ini --yes    # does it
```

A change made through any door is a proposal until you confirm it, so the first command only says what it would do.
With `--yes`, core:

1. imports that one plugin module (the only time a plugin from the catalog is imported),
2. writes its class path into `config.toml`,
3. reloads, and starts the plugin.

```toml
[plugins.formatter.ini]
class = "oaken_plugins.formatter_ini:IniFormatter"
```

`oaken plugin add` takes a plugin's name, an entry-point name, or a class path (`pkg.module:Class`). `--name` gives an
instance its own name and `--config` sets its settings up front as JSON. Secrets are refused there (see
[Secrets](#secrets)).

## Configure a plugin

Settings live in `config.toml`, in the plugin's table, `[plugins.<kind>.<name>]`. Edit the file, or ask core:

```bash
oaken plugin config_get formatter.ini     # the settings as written
oaken plugin config_set formatter.ini ... # change them; core reloads
oaken plugin test formatter.ini --config '{...}'   # try settings on a throwaway copy; nothing is saved
```

`config_set` does not keep a backup of the old file. `oaken config save` keeps a `.bak`. The settings a plugin accepts, with
their types, defaults and which are required, are in `plugin describe`.

A table that sets options for a plugin `oaken-plugins` offers loads it with no `class` line and no `plugin add`:

```toml
[plugins.formatter.rfc822]
strip_attachments = true
```

If a plugin's module fails to load, core logs an error and carries on. A broken plugin never stops startup.

## Template plugins and instances

Some plugins are **templates**: a template runs only as named instances, each with its own settings. One instance per mail
account, per agent, or per set of criteria. `provider.gmail`, `provider.imap` and `provider.smtp` are templates, and so is
the built-in `llm` triager.

An instance is a table that names its template with `plugin`:

```toml
[plugins.triager.llm_fast]
plugin = "llm"
stage = "signal_triage"
criteria = "A company that could buy a vector database."
```

```bash
oaken plugin add triager.llm --name llm_fast --config '{"criteria": "..."}' --yes
oaken plugin remove triager.llm_fast --yes   # removes the instance and its table; the template stays
```

Run bare, a template does nothing. The settings it needs to be an instance are listed under `contextual` in
`plugin describe`.

## Tags and kinds

Every plugin has a **kind** (what it is in the pipeline: provider, formatter, triager, enricher, action, ingestor, store,
embedder, LLM backend and more) and **tags** (what it is for: `mail`, `signal`, `model`, `generic`). The kinds and what each
one must do are in [writing plugins](plugins.md#kinds). Use `--kind` and `--tag` to filter the list.

## Extras

Three plugins need a library that most users do not. They ask for it as an extra:

| Extra | Library | Used by |
|---|---|---|
| `oaken-plugins[imap]` | `imapclient` | the IMAP provider |
| `oaken-plugins[smtp]` | `aiosmtplib` | the SMTP provider |
| `oaken-plugins[clean]` | `selectolax` | the rfc822 formatter's safe mode (HTML to text) |

```bash
pip install 'oaken-plugins[imap,smtp]'
```

A plugin imports its library only when it connects, so you can list it and add it without the extra installed. You only need
it when the plugin runs.

## Secrets

A password or token never goes in `config.toml`, and `plugin add --config` refuses one. Core keeps it in the operating
system's keyring:

```bash
oaken secret set ...      # store a plugin's credential
oaken secret status ...   # is one stored? never shows it
oaken secret delete ...   # remove it
```

See [keyring](keyring.md) for the entry names and what happens when no keyring is available.

## Turn a plugin off and on

```bash
oaken plugin list            # every plugin, whether it started, and why not if it did not
oaken plugin disable KIND.NAME   # stop it and keep it stopped
oaken plugin enable KIND.NAME    # start one that was disabled or switched off for a fault
oaken plugin reload KIND.NAME    # re-read it and start it again
```

## Your own plugins

You do not have to put a plugin in `oaken-plugins`. Core finds one three ways, in this order:

1. A **class path**: `oaken plugin add pkg.module:Class`.
2. An **entry point** from any installed package, or from a folder listed in `[extensions] dirs`. Each folder in `dirs` holds
   plugin folders (`oaken-<kind>-<name>/pyproject.toml`) and is looked in before `oaken-plugins`.
3. The **`oaken-plugins` catalog**.

A name in an extensions folder wins over the same name in `oaken-plugins`. How to write one is in
[writing plugins](plugins.md).

## Versions and compatibility

Each plugin declares the plugin API it speaks and the core versions it needs (`requires_core`). Core refuses one that does
not fit and says why in `plugin list`. The pin between the packages (`oaken-core>=1.0.0,<2` in `oaken-plugins`) keeps pip
from installing a mismatch. Release procedure: [releasing](releasing.md).

## If a plugin does not start

1. `oaken plugin list` says whether it started and, if not, why (`Needs: ...`).
2. `oaken plugin describe KIND.NAME` shows the settings it requires.
3. `oaken plugin test KIND.NAME --config '{...}'` tries settings without saving anything.
4. `oaken log tail` shows the error core logged.
5. A plugin that needs an extra will fail when it connects, not when it is added. Install the extra.
