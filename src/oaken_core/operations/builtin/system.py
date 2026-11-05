# SPDX-License-Identifier: Apache-2.0
"""Status, the change log, plugins and MCP exposure."""

from typing import Any

from pydantic import BaseModel, Field

from oaken_core import paths
from oaken_core.config_edit import refuse_secrets, save_table
from oaken_core.errors import InvalidInput, NotFound
from oaken_core.operations.builtin.queue import Empty, QueueRow
from oaken_core.operations.context import OpContext
from oaken_core.operations.exposure import OP, PLUGIN, scope_of
from oaken_core.operations.registry import Change, Preview, operation
from oaken_core.operations.services import need


class StatusOut(BaseModel):
    version: str
    queues: list[QueueRow]
    plugins_total: int
    plugins_healthy: int


@operation("status", "Version, queues and plugin health at a glance.", permission="read",
           input=Empty, output=StatusOut, mcp="on")  # fmt: skip
async def status(ctx: OpContext, inp: Empty) -> StatusOut:
    host = ctx.svc.host
    statuses = host.statuses() if host else []
    stats = await ctx.svc.queue.stats()
    return StatusOut(version=ctx.svc.version, queues=[QueueRow(**s.__dict__) for s in stats],
                     plugins_total=len(statuses), plugins_healthy=sum(1 for s in statuses if s.healthy))  # fmt: skip


class LogListIn(BaseModel):
    subject_id: str | None = None
    key: str | None = None
    limit: int = Field(default=100, ge=1, le=500)


class LogRowOut(BaseModel):
    ts: str
    actor_kind: str
    actor: str
    subject_id: str
    key: str
    layer: str | None
    op: str
    old_value: str | None
    new_value: str | None


class LogListOut(BaseModel):
    rows: list[LogRowOut]


@operation("log_list", "The change log: who changed what, oldest first.", permission="read",
           input=LogListIn, output=LogListOut, mcp="off", group="log")  # fmt: skip
async def log_list(ctx: OpContext, inp: LogListIn) -> LogListOut:
    rows = await ctx.svc.changelog.rows(ctx.svc.db, inp.subject_id, inp.key, inp.limit)
    return LogListOut(rows=[LogRowOut(**{k: getattr(r, k) for k in LogRowOut.model_fields}) for r in rows])


class PluginRow(BaseModel):
    kind: str
    name: str
    plugin: str
    version: str
    started: bool
    healthy: bool
    enabled: bool
    detail: str


class PluginListOut(BaseModel):
    plugins: list[PluginRow]


@operation("plugin_list", "Every plugin, whether it started, and why not if it did not.",
           permission="read", input=Empty, output=PluginListOut, mcp="on", group="plugin")  # fmt: skip
async def plugin_list(ctx: OpContext, inp: Empty) -> PluginListOut:
    host = need(ctx.svc.host, "the plugin host")
    return PluginListOut(plugins=[PluginRow(**{k: getattr(s, k) for k in PluginRow.model_fields}) for s in host.statuses()])


class PluginRef(BaseModel):
    name: str = Field(description="A plugin or an instance name.")


class PluginAffected(BaseModel):
    affected: list[str]


def _plugin_preview(verb: str):  # type: ignore[no-untyped-def]
    async def preview(ctx: OpContext, inp: PluginRef) -> Preview:
        return Preview(summary=f"{verb} plugin {inp.name}", changes=[Change(target=inp.name, field="plugin", after=verb.lower())], total_changes=1)  # fmt: skip

    return preview


@operation("plugin_enable", "Start a plugin that was disabled or switched off for a fault.",
           permission="mutate", input=PluginRef, output=PluginAffected, mcp="never", group="plugin",
           preview=_plugin_preview("Enable"))  # fmt: skip
async def plugin_enable(ctx: OpContext, inp: PluginRef) -> PluginAffected:
    return PluginAffected(affected=await need(ctx.svc.host, "the plugin host").enable(inp.name))


@operation("plugin_disable", "Stop a plugin and keep it stopped.", permission="mutate",
           input=PluginRef, output=PluginAffected, mcp="never", group="plugin",
           preview=_plugin_preview("Disable"))  # fmt: skip
async def plugin_disable(ctx: OpContext, inp: PluginRef) -> PluginAffected:
    return PluginAffected(affected=await need(ctx.svc.host, "the plugin host").disable(inp.name))


@operation("plugin_reload", "Re-read a plugin and start it again.", permission="mutate",
           input=PluginRef, output=PluginAffected, mcp="never", group="plugin",
           preview=_plugin_preview("Reload"))  # fmt: skip
async def plugin_reload(ctx: OpContext, inp: PluginRef) -> PluginAffected:
    return PluginAffected(affected=await need(ctx.svc.host, "the plugin host").reload(inp.name))


class PluginAddIn(BaseModel):
    kind: str = Field(description="The plugin kind, e.g. embedder or llm_backend.")
    name: str = Field(description="The new instance's name; it is what the config table is called.")
    plugin: str = Field(description="The installed plugin to make an instance of.")
    config: dict[str, Any] = Field(default_factory=dict, description="Its settings. Secrets are refused.")


class PluginAddOut(BaseModel):
    kind: str
    name: str
    started: bool
    detail: str


async def _plugin_add_preview(ctx: OpContext, inp: PluginAddIn) -> Preview:
    problems = need(ctx.svc.host, "the plugin host").check_new_instance(inp.kind, inp.name, inp.plugin, inp.config)
    if problems:
        raise InvalidInput("; ".join(problems))
    changes = [Change(target=f"{inp.kind}.{inp.name}", field="plugin", after=inp.plugin)]
    changes += [Change(target=f"{inp.kind}.{inp.name}", field=k) for k in sorted(inp.config)]  # names only, never values
    return Preview(summary=f"Add {inp.kind} {inp.name} (an instance of {inp.plugin}) and write it to config.toml", changes=changes, total_changes=len(changes))


@operation("plugin_add", "Add a named instance of an installed plugin: checked, written to config.toml, started.",
           permission="mutate", input=PluginAddIn, output=PluginAddOut, mcp="never", group="plugin",
           preview=_plugin_add_preview)  # fmt: skip
async def plugin_add(ctx: OpContext, inp: PluginAddIn) -> PluginAddOut:
    host = need(ctx.svc.host, "the plugin host")
    problems = host.check_new_instance(inp.kind, inp.name, inp.plugin, inp.config)
    if problems:
        raise InvalidInput("; ".join(problems))
    table = {**inp.config, "plugin": inp.plugin}
    save_table(paths.config_dir() / "config.toml", ["plugins", inp.kind, inp.name], table)  # refuses a secret first
    await host.add_instance(inp.kind, inp.name, inp.plugin, inp.config)
    status = next(s for s in host.statuses() if (s.kind, s.name) == (inp.kind, inp.name))
    return PluginAddOut(kind=inp.kind, name=inp.name, started=status.started, detail=status.detail)


class ReloadOut(BaseModel):
    reloading: bool
    note: str


async def _reload_preview(ctx: OpContext, inp: Empty) -> Preview:
    return Preview(summary="Reload core: re-read config.toml and restart every plugin on it. A file with a mistake changes nothing.", changes=[Change(target="core", field="config.toml")], total_changes=1)  # fmt: skip


@operation("core_reload", "Rebuild core on config.toml, in the same process. The file is checked first.",
           permission="mutate", input=Empty, output=ReloadOut, mcp="never", group="core",
           preview=_reload_preview)  # fmt: skip
async def core_reload(ctx: OpContext, inp: Empty) -> ReloadOut:
    need(ctx.svc.reload, "the daemon")()
    return ReloadOut(reloading=True, note="reloading in a moment; the API and console keep their ports")


class PluginTestIn(BaseModel):
    kind: str
    name: str = Field(description="The instance name, as listed by `plugin list`.")
    config: dict[str, Any] = Field(default_factory=dict, description="The settings to try. Nothing is saved.")


class PluginTestOut(BaseModel):
    ok: bool
    detail: str


@operation("plugin_test", "Try settings on a throwaway copy of a plugin and say whether it works. Saves nothing; the running plugin is untouched.",
           permission="read", input=PluginTestIn, output=PluginTestOut, mcp="never", group="plugin")  # fmt: skip
async def plugin_test(ctx: OpContext, inp: PluginTestIn) -> PluginTestOut:
    host = need(ctx.svc.host, "the plugin host")
    refuse_secrets(inp.config)
    row = next((s for s in host.statuses() if (s.kind, s.name) == (inp.kind, inp.name)), None)
    if row is None:
        raise NotFound(f"no {inp.kind} named {inp.name!r}")
    probe = (await host.probe(row.plugin, inp.config, kind=inp.kind))[0]
    return PluginTestOut(ok=probe.ok, detail=probe.detail)


class ExposureRow(BaseModel):
    name: str
    plugin: str | None
    permission: str
    ceiling: str  # the code's mcp mode
    allowed: bool  # may anything turn it on?
    exposed: bool  # is it on now?


class ExposureListOut(BaseModel):
    operations: list[ExposureRow]


@operation("exposure_list", "Which operations MCP may reach, and which are on.", permission="read",
           input=Empty, output=ExposureListOut, mcp="never", group="exposure")  # fmt: skip
async def exposure_list(ctx: OpContext, inp: Empty) -> ExposureListOut:
    exp, chosen = ctx.exposure, await ctx.exposure.settings()
    return ExposureListOut(operations=[
        ExposureRow(name=o.name, plugin=o.plugin, permission=o.permission, ceiling=o.mcp,
                    allowed=exp.allowed(o), exposed=exp.effective(o, chosen))
        for o in ctx.registry.all()
    ])  # fmt: skip


class ExposureSetIn(BaseModel):
    name: str = Field(description="An operation name, or a plugin name with kind=plugin.")
    kind: str = Field(default=OP, pattern="^(op|plugin)$")
    enabled: bool | None = Field(description="None clears the choice back to the default.")


class ExposureSetOut(BaseModel):
    before: bool | None


async def preview_exposure(ctx: OpContext, inp: ExposureSetIn) -> Preview:
    return Preview(summary=f"Turn {inp.kind} {inp.name} {'on' if inp.enabled else 'off' if inp.enabled is False else 'to its default'} for MCP",
                   changes=[Change(target=scope_of(inp.kind, inp.name), field="mcp", after=inp.enabled)], total_changes=1)  # fmt: skip


@operation("exposure_set", "Turn an operation, or a plugin's operations, on or off for MCP.",
           permission="mutate", input=ExposureSetIn, output=ExposureSetOut, mcp="never",
           group="exposure", preview=preview_exposure)  # fmt: skip
async def exposure_set(ctx: OpContext, inp: ExposureSetIn) -> ExposureSetOut:
    if inp.kind == OP:
        op = ctx.registry.get(inp.name)
        if not ctx.exposure.allowed(op):
            raise InvalidInput(f"{inp.name} is never offered over MCP; no setting can change that")
    elif not any(o.plugin == inp.name for o in ctx.registry.all()):
        raise InvalidInput(f"no plugin named {inp.name!r}")
    before = await ctx.exposure.set(scope_of(inp.kind, inp.name), inp.enabled, actor=ctx.caller.actor)
    return ExposureSetOut(before=before)


class UsageIn(BaseModel):
    analyzer: str | None = None


class UsageRow(BaseModel):
    analyzer: str
    model: str
    backend: str
    calls: int
    tokens_in: int
    tokens_out: int
    cache_hits: int
    shortcut_hits: int
    escalations: int
    tokens_saved: int


class SpendToday(BaseModel):
    analyzer: str
    tokens: int
    held_back: int  # jobs deferred today because the analyzer's daily_tokens budget was spent


class UsageOut(BaseModel):
    rows: list[UsageRow]
    spent_today: list[SpendToday]
    calls: int
    tokens_spent: int
    tokens_saved: int
    answered_without_a_model: int  # cache hits plus shortcut hits


@operation("ai_usage", "What the model router spent and saved: calls, tokens, cache and shortcut hits, escalations.",
           permission="read", input=UsageIn, output=UsageOut, mcp="off", group="ai")  # fmt: skip
async def ai_usage(ctx: OpContext, inp: UsageIn) -> UsageOut:
    rows = [UsageRow(**r.__dict__) for r in await need(ctx.svc.router, "the model router").usage(inp.analyzer)]
    spent = [SpendToday(analyzer=r.analyzer, tokens=r.tokens, held_back=r.deferrals) for r in await ctx.svc.router.spent_today(inp.analyzer)]
    return UsageOut(rows=rows, spent_today=spent, calls=sum(r.calls for r in rows), tokens_spent=sum(r.tokens_in + r.tokens_out for r in rows),
                    tokens_saved=sum(r.tokens_saved for r in rows),
                    answered_without_a_model=sum(r.cache_hits + r.shortcut_hits for r in rows))  # fmt: skip


OPERATIONS = (status, log_list, plugin_list, plugin_enable, plugin_disable, plugin_reload,
              exposure_list, exposure_set, ai_usage, plugin_add, plugin_test, core_reload)  # fmt: skip
