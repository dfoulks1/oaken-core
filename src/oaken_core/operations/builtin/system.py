# SPDX-License-Identifier: Apache-2.0
"""Status, the change log, plugins and MCP exposure."""

import tomllib
from typing import Any

from pydantic import BaseModel, Field

from oaken_core import paths
from oaken_core.config_edit import get_table, refuse_secrets, save_table
from oaken_core.errors import InvalidInput, NotFound, PluginError
from oaken_core.operations.builtin.queue import Empty, QueueRow, reload_core
from oaken_core.operations.context import OpContext
from oaken_core.operations.exposure import OP, PLUGIN, scope_of
from oaken_core.operations.registry import Change, Preview, operation
from oaken_core.operations.services import need
from oaken_core.plugins.host import AddPlan


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
    stats = await ctx.svc.jobs.stats()
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
    warning: str = ""  # what it needs that is not running
    hint: str = ""


class PluginListOut(BaseModel):
    plugins: list[PluginRow]


@operation("plugin_list", "Every plugin, whether it started, and why not if it did not.",
           permission="read", input=Empty, output=PluginListOut, mcp="on", group="plugin")  # fmt: skip
async def plugin_list(ctx: OpContext, inp: Empty) -> PluginListOut:
    host = need(ctx.svc.host, "the plugin host")
    return PluginListOut(plugins=[PluginRow(**{k: getattr(s, k) for k in PluginRow.model_fields}) for s in host.statuses()])


class SettingDoc(BaseModel):
    name: str
    type: str
    required: bool
    default: Any = None
    description: str = ""


class CommandDoc(BaseModel):
    name: str
    summary: str
    permission: str


class PluginDocs(BaseModel):
    kind: str
    name: str
    plugin: str
    version: str
    summary: str  # the first line of its docstring
    more: bool  # it says more than that one line: more of a docstring, setup help, an example, settings, commands or contextual settings
    doc: str
    setup_help: str
    config_example: str
    contextual: list[str]  # what an instance must be given (a template)
    template_note: str  # core's own account of what a template is, for a plugin that is one
    settings: list[SettingDoc]
    commands: list[CommandDoc]
    template: bool
    enabled: bool
    started: bool
    healthy: bool
    warning: str  # what it needs that is not running
    hint: str


class PluginDescribeIn(BaseModel):
    name: str | None = Field(default=None, json_schema_extra={"positional": True},
                             description="A plugin or an instance name. Empty: an overview of every plugin in this stack.")  # fmt: skip
    kind: str | None = Field(default=None, description="Only the plugin of this kind, when a name is shared (ollama is a backend and an embedder).")


class PluginDescribeOut(BaseModel):
    text: str  # the same, as plain text for a person
    plugins: list[PluginDocs]


def _type_of(prop: dict[str, Any]) -> str:
    if "type" in prop:
        return str(prop["type"])
    return " or ".join(sorted({str(p.get("type", "?")) for p in prop.get("anyOf", [])})) or "any"


def _docs(ctx: OpContext, host: Any, s: Any) -> PluginDocs:
    d = host.describe(s.kind, s.name)
    schema = host.config_schema(s.kind, s.name) or {}
    need_ = set(schema.get("required", []))
    settings = [SettingDoc(name=n, type=_type_of(p), required=n in need_ or n in d["contextual"], default=p.get("default"), description=str(p.get("description", "")))
                for n, p in (schema.get("properties") or {}).items()]  # fmt: skip
    commands = [CommandDoc(name=o.name, summary=o.summary, permission=o.permission) for o in sorted(ctx.registry.all(), key=lambda o: o.name) if o.plugin in (s.name, s.plugin)]
    rest = "\n".join(d["doc"].strip().splitlines()[1:]).strip()
    more = bool(rest or d["setup_help"] or d["config_example"] or settings or commands or d["contextual"])
    return PluginDocs(kind=s.kind, name=s.name, plugin=s.plugin, version=s.version, summary=(d["doc"].strip().splitlines() or [""])[0].replace("``", ""), more=more, doc=d["doc"].replace("``", "`"),
                      setup_help=d["setup_help"], config_example=d["config_example"], contextual=list(d["contextual"]), template_note=d["template_note"], settings=settings, commands=commands,
                      template=s.template, enabled=s.enabled, started=s.started, healthy=s.healthy, warning=s.warning, hint=s.hint)  # fmt: skip


def _render(plugins: list[PluginDocs], overview: bool) -> str:
    if overview:
        rows = [f"{p.kind:<13}{p.name:<16}{p.summary or '(no description)'}" + (f"  [{p.warning}]" if p.warning else "") for p in plugins]
        return "\n".join([f"{len(plugins)} plugins in this stack. `plugin describe NAME` for one's documentation.", "", *rows])
    out: list[str] = []
    for p in plugins:
        state = "a template" if p.template else "running" if p.healthy else "enabled" if p.enabled else "off"
        out += [f"{p.name} ({p.kind}) v{p.version}  [{state}]", ""]
        if p.doc:
            out += [p.doc.strip(), ""]
        if p.contextual:
            out += [f"An instance of it must be given: {', '.join(p.contextual)}.", *([p.template_note, ""] if p.template_note else [""])]
        if p.setup_help:
            out += ["Setup", f"  {p.setup_help}", ""]
        if p.settings:
            out += ["Settings", *[f"  {s.name}  {s.type}{'  (required)' if s.required else ''}{f'  default {s.default!r}' if s.default not in (None, '') else ''}  {s.description}".rstrip() for s in p.settings], ""]
        if p.commands:
            out += ["Commands", *[f"  {c.name}  {c.summary}" for c in p.commands], ""]
        if p.config_example:
            out += ["Example config.toml", *[f"  {line}" for line in p.config_example.splitlines()], ""]
        if p.warning:
            out += [f"Needs: {p.warning}. {p.hint}".rstrip(), ""]
    return "\n".join(out).rstrip() + "\n"


@operation("plugin_describe", "A plugin's own documentation (what it is, how to set it up, its settings and commands), from the plugin itself, for the plugins in this stack.",
           permission="read", input=PluginDescribeIn, output=PluginDescribeOut, mcp="on", group="plugin")  # fmt: skip
async def plugin_describe(ctx: OpContext, inp: PluginDescribeIn) -> PluginDescribeOut:
    host = need(ctx.svc.host, "the plugin host")
    found = [s for s in host.statuses() if (inp.name is None or inp.name in (s.name, s.plugin)) and (inp.kind is None or s.kind == inp.kind)]
    if inp.name is not None and not found:
        raise NotFound(f"no plugin named {inp.name!r} in this stack: `plugin list` shows what is installed, `plugins` what could be added")
    plugins = [_docs(ctx, host, s) for s in found]
    return PluginDescribeOut(text=_render(plugins, overview=inp.name is None), plugins=plugins)


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
    plugin: str = Field(json_schema_extra={"positional": True},
                        description="What to add: a plugin's name (installed, or a directory in [extensions] dirs), or a class path such as pkg.module.Class.")  # fmt: skip
    kind: str | None = Field(default=None, description="The plugin kind. Only needed when the name is shared or a class does not say.")
    name: str | None = Field(default=None, description="A name for this instance. Empty: the plugin's own name, which switches it on.")
    config: dict[str, Any] = Field(default_factory=dict, description="Its settings. Secrets are refused.")


class PluginAddOut(BaseModel):
    kind: str
    name: str
    started: bool
    detail: str
    reloading: bool = False


def _plan(ctx: OpContext, inp: PluginAddIn) -> AddPlan:
    try:
        return need(ctx.svc.host, "the plugin host").plan_add(inp.plugin, kind=inp.kind, name=inp.name, config=inp.config)
    except PluginError as exc:
        raise InvalidInput(exc.message) from None


async def _plugin_add_preview(ctx: OpContext, inp: PluginAddIn) -> Preview:
    plan = _plan(ctx, inp)
    where = f"{plan.kind}.{plan.name}"
    changes = [Change(target=where, field=k, after=v if k in ("plugin", "class", "enabled") else None) for k, v in sorted(plan.table.items())]  # names only, never values
    then = " and reload core to find it" if plan.how == "reload" else ""
    return Preview(summary=f"Add {plan.kind} {plan.name} ({inp.plugin}), write it to config.toml{then}", changes=changes, total_changes=len(changes))


@operation("plugin_add", "Add a plugin by name (installed, or in an extensions directory) or by class path: written to config.toml, started, and core reloaded if it has to find it.",
           permission="mutate", input=PluginAddIn, output=PluginAddOut, mcp="never", group="plugin",
           preview=_plugin_add_preview)  # fmt: skip
async def plugin_add(ctx: OpContext, inp: PluginAddIn) -> PluginAddOut:
    host = need(ctx.svc.host, "the plugin host")
    plan = _plan(ctx, inp)
    path = paths.config_dir() / "config.toml"
    have = get_table(path.read_text(encoding="utf-8") if path.exists() else "", ["plugins", plan.kind, plan.name])
    save_table(path, ["plugins", plan.kind, plan.name], {**have, **plan.table})  # refuses a secret first
    if plan.how == "reload":
        reloading, note = reload_core(ctx, "the plugin starts in a moment")
        return PluginAddOut(kind=plan.kind, name=plan.name, started=False, detail=note, reloading=reloading)
    await host.apply_add(plan)
    status = next(s for s in host.statuses() if (s.kind, s.name) == (plan.kind, plan.name))
    return PluginAddOut(kind=plan.kind, name=plan.name, started=status.started, detail=status.detail)


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
    group: str = ""  # the CLI group it sits under ("" for a core operation with none)
    summary: str = ""
    provider: str = ""  # who offers it: ``core.provider.<group>``, or the plugin class's dotted path


class ExposureListOut(BaseModel):
    operations: list[ExposureRow]


def provider_of(ctx: OpContext, op: Any) -> str:
    if op.plugin is None:
        return f"core.provider.{op.group or 'general'}"
    host = ctx.svc.host
    return (host.provider_of(op.plugin) if host is not None else "") or op.plugin


@operation("exposure_list", "Which operations MCP may reach, and which are on.", permission="read",
           input=Empty, output=ExposureListOut, mcp="never", group="exposure")  # fmt: skip
async def exposure_list(ctx: OpContext, inp: Empty) -> ExposureListOut:
    exp, chosen = ctx.exposure, await ctx.exposure.settings()
    return ExposureListOut(operations=[
        ExposureRow(name=o.name, plugin=o.plugin, permission=o.permission, ceiling=o.mcp,
                    allowed=exp.allowed(o), exposed=exp.effective(o, chosen), group=o.group, summary=o.summary,
                    provider=provider_of(ctx, o))
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


class McpPowerIn(BaseModel):
    enabled: bool = Field(description="Whether the person's MCP door serves anything.")


class McpPowerOut(BaseModel):
    enabled: bool
    note: str


async def preview_mcp_power(ctx: OpContext, inp: McpPowerIn) -> Preview:
    return Preview(summary=f"Turn the MCP door {'on' if inp.enabled else 'off'} (the agent door has its own switch)",
                   changes=[Change(target="api", field="mcp_enabled", before=ctx.svc.settings.api.mcp_enabled, after=inp.enabled)], total_changes=1)  # fmt: skip


@operation("mcp_power", "Turn the person's MCP door on or off. Off: it answers 503 and offers no tools. Saved to config.toml and live at once.",
           permission="mutate", input=McpPowerIn, output=McpPowerOut, mcp="never", group="mcp",
           preview=preview_mcp_power)  # fmt: skip
async def mcp_power(ctx: OpContext, inp: McpPowerIn) -> McpPowerOut:
    path = paths.config_dir() / "config.toml"
    try:
        table = dict(tomllib.loads(path.read_text(encoding="utf-8")).get("api", {}))
    except FileNotFoundError:
        table = {}
    save_table(path, ["api"], {**table, "mcp_enabled": inp.enabled})
    ctx.svc.settings.api.mcp_enabled = inp.enabled  # the door reads this on every request
    return McpPowerOut(enabled=inp.enabled, note="the MCP door is " + ("on" if inp.enabled else "off"))


OPERATIONS = (status, log_list, plugin_list, plugin_describe, plugin_enable, plugin_disable, plugin_reload,
              exposure_list, exposure_set, mcp_power, ai_usage, plugin_add, plugin_test, core_reload)  # fmt: skip
