+++
title = "Writing a plugin"
subtitle = "Kinds, base classes, commands, templates and tests"
lead = "Everything core does beyond its pipeline is a plugin: a small class, registered with a Python entry point. Start from a base class, override what your plugin does, and run the contract suite."
badges = [["text-bg-warning", "Alpha"]]
group = "concepts"
order = 30
description = "How to write a plugin for core: kinds, BasePlugin and @command, template plugins, config_model, tags, migrations, labels, versions, the UI contract, contract tests and entry points."
+++

## Kinds

A kind is a role. `repo:src/oaken_core/plugins/kinds.py` lists the ones core ships. Each names an
entry-point group and the members a plugin of that kind must have.

| Kind | Entry-point group | Must have |
| --- | --- | --- |
| `store` | `oaken_core.stores` | `open` (one active; starts first) |
| `auth` | `oaken_core.auth` | `authenticate`, `authorize` (one active) |
| `gateway` | `oaken_core.gateways` | `serve` |
| `provider` | `oaken_core.providers` | nothing (it calls `submit`) |
| `llm_backend` | `oaken_core.llm_backends` | `model_id`, `context_window`, `generate` |
| `embedder` | `oaken_core.embedders` | `model_id`, `dimensions`, `embed` |
| `vector_store` | `oaken_core.vector_stores` | `upsert`, `delete`, `query`, `stats` |
| `cache_store` | `oaken_core.cache_stores` | `key`, `get`, `put`, `stats`, `clear` |
| `triager` | `oaken_core.triagers` | `version`, `needs_llm`, `rank`, `triage` |
| `enricher` | `oaken_core.enrichers` | `version`, `needs_llm`, `enrich` |
| `action` | `oaken_core.actions` | `version`, `needs_llm`, `routes`, `act` |
| `formatter` | `oaken_core.formatters` | `formats`, `split` |
| `ingestor` | `oaken_core.ingestors` | `ingest` (one active) |

Triagers, enrichers and actions are **analyzers**: each is the stage of a queue, and they take part in
the `needs` graph. The list is not closed. A product adds a kind with `KindRegistry.register`.

The base classes are optional. A kind is a shape, and any object with the right members works. The
host checks the same rules either way. The bases (`BaseTriager`, `BaseEmbedder`, `BaseLLMBackend`
and the rest in `repo:src/oaken_core/plugins/base.py`) give you defaults and tracing.

## A plugin in one screen

A triager that rejects items with too little text. The test below runs against the code as written
here.

```python
from typing import ClassVar

from oaken_core.intake import IntakeItem, Verdict, item_text
from oaken_core.plugins.base import BaseTriager, command
from oaken_core.plugins.contracts import PluginOpContext
from pydantic import BaseModel, Field


class ShortConfig(BaseModel):
    min_chars: int = Field(default=20, ge=1, description="Items shorter than this are rejected.")


class CheckIn(BaseModel):
    text: str


class CheckOut(BaseModel):
    verdict: str


class ShortTriager(BaseTriager):
    """Rejects items with too little text."""

    api_version = 1
    requires_core = ">=0.0.0"
    version: ClassVar[str] = "1"
    rank: ClassVar[int] = 20
    tags: ClassVar[tuple[str, ...]] = ("example",)
    config_model = ShortConfig

    async def triage(self, item: IntakeItem) -> Verdict | None:
        if len(item_text(item)) < self.config.min_chars:
            return Verdict("reject", "too short")
        return None  # no opinion: the next triager decides

    @command("check", "Say what this triager would do with some text.", input=CheckIn, output=CheckOut)
    async def check(self, ctx: PluginOpContext, inp: CheckIn) -> CheckOut:
        v = await self.triage(IntakeItem("s", "try", inp.text, {}))
        return CheckOut(verdict=v.kind if v else "pass")
```

```python
from oaken_core import __version__
from oaken_core.plugins.kinds import KindRegistry
from oaken_core.plugins.validate import check_plugin, check_started
from oaken_core.testing.contracts import running


async def test_short_triager() -> None:
    spec = KindRegistry.shipped().get("triager")
    _, problems = check_plugin(spec, "short", ShortTriager(), __version__)
    assert not problems
    async with running(ShortTriager(), "triager", {"min_chars": 5}) as t:
        assert not check_started(spec, t)
        assert (await t.triage(IntakeItem("s1", "x", "hi", {}))).kind == "reject"
        assert await t.triage(IntakeItem("s2", "x", "long enough", {})) is None
        assert [op.name for op in t.operations()] == ["check"]
```

`check_plugin` is what the host runs at load. `running` starts the plugin as the host does and stops
it after. With `asyncio_mode = "auto"` in pytest, the test needs no decorator.

## What a base class gives you

`BasePlugin` carries class attributes the host reads without importing your code's side effects:

| Attribute | Meaning |
| --- | --- |
| `kind` | Set by the kind's base (`triager`, `llm_backend`...). |
| `version` | Your plugin's version. For an analyzer it is part of the answer cache key: bump it when the output could change. |
| `api_version` | The plugin API you wrote against. See below. |
| `requires_core` | A version range of core (`">=0.1,<0.2"`). Empty: any. The host refuses a plugin outside it. |
| `config_model` | A pydantic model of your settings. `start()` parses `ctx.config` into `self.config` and raises a `PluginError` naming the bad fields. The JSON Schema builds the console's settings form. |
| `requires_config` | Settings you cannot run without. While one is missing the plugin stays off, with the reason shown, and starts when it is set. |
| `enabled_default` | `False` makes the plugin opt-in (it costs money or reaches a network). |
| `fallback` | `True`: used only while no other plugin of its kind is installed. |
| `needs` | For an analyzer, the stored results it reads (`"<analyzer>@<relation>"`). Must be acyclic. |
| `tags` | What the plugin is for beyond its kind (`("mail",)`). `oaken plugins --tag` filters on it. |
| `migrations_dir` | A folder of `NNNN_name.sql` files for tables you own. |
| `setup_help`, `config_example` | Plain words and a config snippet, shown where people set the plugin up. |

Methods you may override: `start(ctx)`, `stop()`, `health()`, `labels()`, `ui()`, `row()`,
`operations()`, `job_handlers()`, `scheduled_tasks()`, `setting_choices()`. Every call a plugin
implements from the kind's contract (`generate`, `embed`, `triage`, and so on) is traced with no
code in the plugin. `self.trace(kind, **data)` adds an event of your own.

Plugins run in-process and are not sandboxed. The contract keeps the code tidy. It does not contain a
hostile plugin. Install only what you trust.

### Commands

`@command(name, summary, input=, output=)` offers a method as an operation named `<plugin>.<name>`.
It is in the operation registry like any core operation, so the CLI builds a verb for it
(`oaken <plugin> <name>`, one flag per input field). `permission` is `read` (default) or `mutate`
(confirmed). For a `mutate`, `preview` names a method that says what would change. A command is off
over MCP until the user turns it on (`mcp="never"` keeps it off for good). A plugin that fails to load
takes its commands with it.

Do not write `enable`, `disable`, `reload` or settings commands. Core does those for every plugin
(the operations `plugin_enable`, `plugin_disable`, `plugin_reload`).

### Tables, labels and Deferred

- **Tables.** Set `migrations_dir` and name your tables `<plugin>_...`. The host applies them once
  per plugin (with its own version table), in the same run as core's, before `start`. You get `ctx.db`.
- **Labels.** `labels()` returns the label keys you define. Core reserves `queue`, `priority` and
  `confidential`. A clash switches the plugin off.
- **Deferred.** Raise `oaken_core.errors.Deferred(message, retry_after)` when something you need is
  down or spent. The job goes back to the queue and the attempt is not counted.

### Asking a model

An analyzer that needs a model adds `BaseLLMAnalyzer` (`class Mine(BaseLLMAnalyzer, BaseTriager)`),
declares `prompt`, `guard` and a default `route`, and calls `self.ask(request, Answer, content_hash=...)`.
See the [router](architecture/router.md) and [prompt safety](architecture/prompt-safety.md). For
embeddings and nearest-example lookups add `BaseVecAnalyzer`. `BaseMultitenant` says several workers
may run the plugin at once.

## Template plugins

A template runs only as named instances, each given what makes it what it is. The `llm` triager is
one: every instance has its own `criteria`.

```python
class Mine(BaseTemplatePlugin, BaseTriager):
    contextual = ("criteria",)
```

`contextual` lists the settings an instance must set. A template with an empty `contextual` raises
`TypeError` at class creation. Bare, the plugin never starts. The console shows a template pill, and
a **+** makes an instance. An entry `a|b` means one of the two is enough. An instance is a config
table that says which template it is:

```toml
[plugins.triager.llm_fast]
plugin = "llm"
stage = "signal_triage"
criteria = "A company that could buy a vector database."
```

Tables are `[plugins.<kind>.<name>]`. The keys `enabled`, `plugin`, `class`, `stage`, `locality`,
`concurrency`, `route`, `prompt` and `guard` belong to the host. Your `ctx.config` never holds them.
`class = "pkg.mod:Class"` loads a class with no entry point. Use `setup_help` and `config_example` to
say what the template is for.

## The plugin API version

`PLUGIN_API_VERSION` (now 1, in `repo:src/oaken_core/plugins/contracts.py`) is the shape of the
contracts. A plugin whose `api_version` differs is refused, and the reason shows beside it. Plugin
authors should set it explicitly, as the example does. `requires_core` is the separate, finer check
against core's own version. See [ADR 0010](repo:docs/origin/0010-the-stable-surface-and-its-versioning.md).

## The UI contract

A plugin declares its console UI as data, never markup ([ADR 0018](repo:docs/origin/0018-a-web-ui-contract-for-plugins.md)).
Override `ui()` and return a `UiSpec` (`repo:src/oaken_core/plugins/ui.py` checks it):

| Part | Does |
| --- | --- |
| `fields` | Overrides for settings the config schema already made (label, kind, help, choices). |
| `sections`, `buttons` | Group settings; run one of the plugin's own operations (no input). |
| `pages` | A form core serves: `load` (a read operation), optional `test`, `submit`, and `order` for a list the user reorders by typing positions. |
| `template` | Set by `BaseTemplatePlugin.template_ui()`. |
| `row` | The pills and buttons in the plugin's table row (`row()`). |

A field kind is `text`, `number`, `bool`, `enum`, `list` or `textarea`. A button or page may call only
the plugin's own operations. Core owns the HTML, escaping and CSRF. If `check_ui` finds a problem, core
ignores the spec and builds the settings modal from the schema. Without `ui()`, that is what you get.

## Testing

Contract suites live in `repo:src/oaken_core/testing/contracts.py`. There is one for each of four
kinds: `EmbedderContract`, `LLMContract`, `VectorStoreContract` and `CacheContract`. Use one as a
pytest mixin and override the fixture:

```python
class TestMyEmbedder(EmbedderContract):
    @pytest.fixture
    def embedder(self):
        return MyEmbedder()
```

Or in one call, `await verify_embedder(MyEmbedder)` (also `verify_llm`, `verify_vector_store`,
`verify_cache`), which runs every rule and raises one error listing all failures. Add
`embedder_config` (and `llm_config`, `store_config`) for a plugin that needs settings. The suites check
the host's own validity rules too. Other kinds have no suite yet: test them as the example does.

`repo:src/oaken_core/router/stub.py` gives a scripted `StubBackend`, a `StubEmbedder` and
`MemoryVectors` for testing code that uses a model, with no network.

## Registering it

Add an entry point in your package's `pyproject.toml`, in the group of your kind. The entry-point
name is the plugin's name. Core's own plugins do exactly this.

```toml
[project.entry-points."oaken_core.triagers"]
short = "my_package.short:ShortTriager"
```

Install the package in the same environment as core and core finds it. `oaken plugin add KIND.NAME`
writes the config table. `oaken plugins` lists the plugins in the `oaken-plugins` package and your
`[extensions] dirs`, with `--kind` and `--tag` filters.

## What the host does

- **Load.** Imports the plugin and checks it: `api_version`, `requires_core`, the members of its
  kind, its commands, jobs and UI. It reports every problem at once.
- **Start.** Calls `start(ctx)`. The store starts first, then core's migrations, then the rest.
- **Watch.** Polls `health()`. A plugin that fails to load, start or answer is marked unhealthy,
  logged, announced as `PluginHealthChanged`, and left out of lookups. One whose `start()` failed is
  retried at each check.
- **Stop.** Calls `stop()` once.

Core never stops because a plugin did.
