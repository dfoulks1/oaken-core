# SPDX-License-Identifier: Apache-2.0
"""The console: core's own web UI. Not a plugin, so it cannot be switched off or broken by one.

Pages are server-rendered Jinja with no script. Every mutation is a form post that carries the
console's CSRF token and goes through the same operation gate as the CLI (a ``mutate`` shows what it
would change and waits for a second click). A plugin reaches the console only as data: its
``UiSpec`` (ADR 0018), drawn here as forms, and served as JSON beside. A plugin without a spec, or
with a malformed one (the host already refused it), gets a form built from its config schema.

Written for core. Ideas kept from oaken-mail's console: the page families (general, plugin pages,
pipeline), the position-box ordering (``plugins.ui.reposition``) and the Origin null form-post rule
(in ``guard``). Not lifted: its templates, which are mail copy end to end.
"""

import dataclasses
from urllib.parse import urlencode
import re
import time
import hashlib
import hmac
import os
import secrets
import tomllib
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import jinja2
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from oaken_core import identity, paths
from oaken_core.api.errors import status_for
from oaken_core.config_edit import get_table, refuse_secrets, save_table, set_table
from oaken_core.console import forms
from oaken_core.console.docs_text import docs_html
from oaken_core.console.forms import Field
from oaken_core.console.guard import ConsoleGuard, PortRef
from oaken_core.errors import ConfigError, InvalidInput, NotFound, OakenError, PermissionDenied
from oaken_core.logging import get_logger
from oaken_core.operations.caller import CallerContext
from oaken_core.operations.client import CoreClient
from oaken_core.operations.registry import PLUGIN_SEP
from oaken_core.operations.services import Services
from oaken_core.plugins.contracts import UiPage, UiSpec
from oaken_core.plugins.host import HOST_KEYS
from oaken_core.plugins.ui import reposition
from oaken_core.queues import DEFAULT

log = get_logger("oaken_core.console")
HERE = Path(__file__).resolve().parent
UI = CallerContext.ui()
PALETTES = ("auto", "oak", "maple", "birch", "acorn", "moss", "stone", "sky", "stream", "contrast")
MODES = ("auto", "light", "dark")
SCREEN_ROWS = 24  # table rows (headers too) that fit a screen; past this, a page's folds start collapsed
CSS_VERSION = hashlib.sha256((HERE / "static" / "oaken.css").read_bytes()).hexdigest()[:10]
# The Capabilities page's core tables: three, so a new command group does not mean a new table. A group named nowhere here
# (a product's own) lands in "Other". Plugins keep a table per kind.
CAPABILITY_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Control", ("queue", "schedule", "job", "label", "intake", "item")),
    ("Machinery", ("general", "plugin", "config", "settings", "core", "storage", "cache", "ai", "log", "events")),  # "general": status
    ("Keys and doors", ("agent", "secret", "exposure", "mcp")),
)
_here: ContextVar[str] = ContextVar("console_here", default="/")  # the page being drawn, for nav and "back"
_test: ContextVar[str] = ContextVar("console_test", default="")  # ``?test=`` on the page: which Test result to show in its dialog


Handler = Callable[[Request], Awaitable[Response]]


# One token for the life of the process, so a page left open across a core reload still posts.
_PROCESS_TOKEN = secrets.token_urlsafe(32)


def _unfit(out: dict[str, Any]) -> str:
    return "Loads, but these stay disabled until fixed: " + "; ".join(out["warnings"])


class Console:
    """An ASGI app. ``bind(port)`` is called once the socket is open (the Host check needs it)."""

    def __init__(self, client: CoreClient, svc: Services) -> None:
        self.client, self.svc = client, svc
        self.token = _PROCESS_TOKEN
        self._tests: dict[str, dict[str, Any]] = {}  # recent Test results by token, for the page they return to
        self.look = {"palette": svc.settings.console.palette, "mode": svc.settings.console.mode}
        self.port = PortRef()
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True, undefined=jinja2.StrictUndefined
        )
        self.env.globals["display"] = forms.display  # macros do not see the page's context
        self.env.filters["docs"] = docs_html  # a plugin's own text as paragraphs and code, escaped
        self.config_path = paths.config_dir() / "config.toml"
        routes = [
            Route("/", self.page(self.overview)),
            Route("/plugins", self.page(self.plugins)),
            Route("/log", self.page(self.log_page)),
            Route("/log/export", self.page(self.log_export)),
            Route("/pipeline", self.page(self.providers)),
            Route("/pipeline/providers", self.page(self.providers)),
            Route("/pipeline/labels", self.page(self.labels_page)),
            Route("/pipeline/analyzers", self.page(self.analyzers)),
            Route("/pipeline/machinery", self.page(self.pipeline)),
            Route("/ui/labels/new", self.post(self.label_verb), methods=["POST"]),
            Route("/ui/labels/{key}/{verb}", self.post(self.label_verb), methods=["POST"]),
            Route("/capabilities", self.page(self.capabilities)),
            Route("/stage/{queue}", self.page(self.stage)),
            Route("/plugins/{kind}/{name}", self.page(self.plugin)),
            Route("/plugins/{kind}/{name}/pages/{page}", self.page(self.plugin_page)),
            Route("/queues", self.page(self.queues)),
            Route("/config", self.page(self.config_file)),
            Route("/json/status", self.data(self.j_status)),
            Route("/json/plugins", self.data(self.j_plugins)),
            Route("/json/plugins/{kind}/{name}/ui", self.data(self.j_ui)),
            Route("/json/queues", self.data(self.j_queues)),
            Route("/json/config", self.data(self.j_config)),
            Route("/ui/look", self.post(self.set_look), methods=["POST"]),
            Route("/ui/queues/add", self.post(self.add_queue), methods=["POST"]),
            Route("/ui/queues/{queue}/{verb}", self.post(self.queue_verb), methods=["POST"]),
            Route("/ui/jobs/{job}/requeue", self.post(self.job_requeue), methods=["POST"]),
            Route("/ui/queues/{queue}/plugins/{name}/{state}", self.post(self.stage_plugin), methods=["POST"]),
            Route("/ui/expose", self.post(self.expose), methods=["POST"]),
            Route("/ui/mcp-power", self.post(self.mcp_power), methods=["POST"]),
            Route("/ui/config-file/test", self.post(self.config_file_test), methods=["POST"]),
            Route("/ui/config-file/save", self.post(self.config_file_save), methods=["POST"]),
            Route("/ui/config-file/reload", self.post(self.config_file_reload), methods=["POST"]),
            Route("/ui/plugins/{kind}/{name}/settings", self.post(self.save_settings), methods=["POST"]),
            Route("/ui/plugins/{kind}/{name}/instance", self.post(self.new_instance), methods=["POST"]),
            Route("/ui/plugins/{kind}/{name}/remove", self.post(self.remove_instance), methods=["POST"]),
            Route("/ui/plugins/{kind}/{name}/try", self.post(self.try_settings), methods=["POST"]),
            Route("/ui/plugins/{kind}/{name}/button", self.post(self.press), methods=["POST"]),
            Route("/ui/plugins/{kind}/{name}/pages/{page}/{action}", self.post(self.submit_page), methods=["POST"]),
            Route("/ui/plugins/{kind}/{name}/{verb}", self.post(self.plugin_verb), methods=["POST"]),
            Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
        ]
        self.app = ConsoleGuard(Starlette(routes=routes), self.port, lambda: self.svc.host.console_names() if self.svc.host else frozenset())

    def bind(self, port: int) -> None:
        self.port.value = port

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self.app(scope, receive, send)

    # -- plumbing ------------------------------------------------------------------------------
    def _addable(self) -> list[str]:
        """The analyzer kinds, for the Add a stage button (a kind may have several stages); none when a product fixes its own."""
        if not self.svc.queues_from_config or self.svc.host is None:
            return []
        return [k.name for k in self.svc.host.kinds if k.analyzer]

    def render(self, template: str, http: int = 200, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("addable", self._addable())
        ctx.setdefault("test", self._tests.get(_test.get()))
        html = self.env.get_template(template).render(
            nav=self._nav(), app_title=identity.title, wordmark=identity.wordmark(), token=self.token, csrf=self.token, version=self.svc.version, look=self.look, palettes=PALETTES,
            modes=MODES, css_version=CSS_VERSION, here=_here.get(), **ctx)
        if html.count("<tr") > SCREEN_ROWS:  # longer than a screen: every fold starts shut, so the page opens as a list of titles
            html = re.sub(r'(<details class="card fold"[^>]*?) open>', r"\1>", html)
        return HTMLResponse(html, status_code=http)

    def fail(self, exc: BaseException) -> Response:
        if isinstance(exc, OakenError):
            return self.render("error.html", status_for(exc), code=exc.code, message=exc.message)
        log.error("console handler failed", error=type(exc).__name__)
        return self.render("error.html", 500, code="internal", message="something went wrong inside core; see its log")

    def page(self, fn: Handler) -> Handler:
        async def wrapped(request: Request) -> Response:
            _here.set(request.url.path)
            _test.set(request.query_params.get("test", ""))
            try:
                return await fn(request)
            except Exception as exc:  # noqa: BLE001 - every error becomes a page, never a crash
                return self.fail(exc)

        return wrapped

    def data(self, fn: Callable[[Request], Awaitable[Any]]) -> Handler:
        async def wrapped(request: Request) -> Response:
            try:
                return JSONResponse(await fn(request))
            except OakenError as exc:
                return JSONResponse({"code": exc.code, "message": exc.message}, status_code=status_for(exc))
            except Exception as exc:  # noqa: BLE001
                log.error("console json failed", error=type(exc).__name__)
                return JSONResponse({"code": "internal", "message": "see core's log"}, status_code=500)

        return wrapped

    def post(self, fn: Callable[[Request, dict[str, str]], Awaitable[Response]]) -> Handler:
        """Every mutation: the form must carry the console's token. The form is read once and handed on."""

        async def wrapped(request: Request) -> Response:
            try:
                form = {k: str(v) for k, v in (await request.form()).items()}
                _here.set(form.pop("back", ""))  # the page the form came from (empty: it did not say)
                if not hmac.compare_digest(form.pop("csrf", ""), self.token):
                    raise PermissionDenied("this form is missing its token; reload the page and try again")
                return await fn(request, form)
            except Exception as exc:  # noqa: BLE001
                return self.fail(exc)

        return wrapped

    # -- reading core --------------------------------------------------------------------------
    def _host(self) -> Any:
        if self.svc.host is None:
            raise NotFound("the plugin host is not running")
        return self.svc.host

    def _status(self, kind: str, name: str) -> Any:
        for s in self._host().statuses():
            if (s.kind, s.name) == (kind, name):
                return s
        raise NotFound(f"no {kind} plugin named {name!r}")

    def _values(self, kind: str, name: str) -> dict[str, Any]:
        return {k: v for k, v in self._host().effective(kind, name).items() if k not in HOST_KEYS}  # what it runs on, inherited included

    async def _op(self, name: str, payload: Any = None) -> dict[str, Any]:
        out = (await self.client.call(name, payload or {}, UI)).output
        return out.model_dump(mode="json") if isinstance(out, BaseModel) else {}

    def _modal(self, kind: str, name: str) -> dict[str, Any]:
        """What the settings modal shows: the plugin's spec (if it has a sound one) over its config
        schema's fields, else the schema alone."""
        host = self._host()
        schema = host.config_schema(kind, name)
        fields = forms.from_schema(schema) if schema else []
        spec: UiSpec | None = host.plugin_ui(kind, name)
        by_name = {f.name: f for f in fields}
        sections: list[tuple[str, list[Field]]] = []
        if spec is not None and spec.sections:
            used: set[str] = set()
            for s in spec.sections:
                sections.append((s.title, [by_name[n] for n in s.fields if n in by_name]))
                used |= set(s.fields)
            rest = [f for f in fields if f.name not in used]
            if rest:
                sections.append(("Other", rest))
        elif fields:
            sections.append(("Settings", fields))
        return {"spec": spec, "schema": schema, "fields": fields, "sections": sections}

    # -- pages ---------------------------------------------------------------------------------
    def _stage_specs(self) -> list[Any]:
        """The queues that are a stage (they serve an analyzer kind), in the order they were made."""
        return [q for q in self.svc.queues.all() if q.kind]

    def _nav(self) -> list[tuple[str, str]]:
        """General, the machinery, then a link per stage in the order they were made, then Config and Capabilities."""
        stages = [(f"/stage/{q.name}", q.title) for q in self._stage_specs()]
        return [("/", "General"), ("/pipeline", "Pipeline"), *stages, ("/config", "Config"), ("/capabilities", "Capabilities")]

    async def _flow(self) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """Intake, then each stage, with what waits in it; and the queue rows by name."""
        rows = {r["queue"]: r for r in (await self._op("queue_list"))["queues"]}
        out = []
        for q in self.svc.queues.all():
            if q.kind:
                r = rows.get(q.name, {})
                done = (await q.metrics(3600)).succeeded  # what finished in the last hour: a quick job leaves nothing waiting
                out.append({"name": q.name, "done": done, "title": q.title,
                            "href": f"/stage/{q.name}", "queued": r.get("queued", 0),
                            "failed": r.get("failed", 0), "paused": r.get("paused", False)})
        return out, rows

    @staticmethod
    def _back(default: str) -> str:
        """Where a form came from (its ``back`` field), if that is a page on this console; else ``default``."""
        back = _here.get()
        return back if back.startswith("/") and not back.startswith("//") else default

    async def _choices(self, kind: str, name: str, modal: dict[str, Any]) -> dict[str, Any]:
        """``modal`` with each setting the plugin offers choices for drawn as a dropdown (a plain text setting only)."""
        found = await self._host().setting_choices(kind, name)
        if not found:
            return modal

        def pick(f: Field) -> Field:
            return dataclasses.replace(f, kind="enum", choices=tuple(found[f.name])) if f.name in found and f.kind == "text" else f

        return {**modal, "fields": [pick(f) for f in modal["fields"]], "sections": [(t, [pick(f) for f in fs]) for t, fs in modal["sections"]]}

    def _log_scope(self, request: Request) -> dict[str, str]:
        """The scope a Log page was asked for: ``?plugin=kind/name``, ``?stage=queue``, or neither for all of it."""
        return {k: request.query_params[k] for k in ("plugin", "stage", "level") if request.query_params.get(k)}

    async def log_page(self, request: Request) -> Response:
        """The log as plain text on a page of its own. Where the file is comes first when there is one; JSON can be exported."""
        scope = self._log_scope(request)
        out = await self._op("log_tail", {**scope, "limit": 500})
        back = f"/plugins/{scope['plugin']}" if "plugin" in scope else f"/stage/{scope['stage']}" if "stage" in scope else "/"
        widen: list[tuple[str, dict[str, str]]] = []  # the log in a wider scope: the plugin's stage, then everything
        if "plugin" in scope:
            kind, _, name = scope["plugin"].partition("/")
            if name and (stage := self._stage_choice(kind, name)["stage"]):
                widen.append(("Its stage", {"stage": stage}))
        if scope:
            widen.append(("Everything", {}))
        return self.render("log.html", log=out, scope=scope, back=back, export=urlencode(scope), title=scope.get("plugin") or scope.get("stage") or "Everything", widen=widen)

    async def log_export(self, request: Request) -> Response:
        """The same lines as a JSON file to save: what ``log_tail`` returned, as it returned it."""
        out = await self._op("log_tail", {**self._log_scope(request), "limit": 2000})
        return JSONResponse(out, headers={"Content-Disposition": 'attachment; filename="oaken-log.json"'})

    async def _plugin_docs(self, kind: str, name: str) -> dict[str, Any]:
        """The plugin's own account of itself, and the commands it offers (from the registry, with their summaries)."""
        found = (await self._op("plugin_describe", {"name": name, "kind": kind}))["plugins"]
        return next((p for p in found if p["name"] == name), {"more": False, "doc": "", "setup_help": "", "config_example": "", "contextual": [], "commands": []})

    def _bases(self, kind: str, only: str = "") -> list[dict[str, Any]]:
        """The plugins of ``kind`` a new instance can be made of, each with the contextual settings it must be given (required)."""
        out = []
        for p in self._host().statuses():
            if p.kind != kind or p.plugin != p.name or (only and p.name != only):
                continue
            must = set(self._host().contextual(kind, p.name))
            if not must or not p.enabled:  # only an enabled template (a plugin that needs something to be one) gets the +
                continue
            try:
                fields = [dataclasses.replace(f, required=True) for f in self._modal(kind, p.name)["fields"] if f.name in must]
            except OakenError:
                fields = []
            out.append({"kind": kind, "name": p.name, "plugin": p.plugin, "template": p.template, "fields": fields})
        return out

    async def _route_choice(self, kind: str, name: str) -> dict[str, Any]:
        """For a plugin that asks a model: the models the running backends report (``backend:model``), so the dialog offers what
        exists, and the one this plugin uses now. Empty for any other plugin."""
        host = self._host()
        if host.llm_defaults(kind, name) is None:
            return {}
        models: list[tuple[str, str]] = []
        for p in host.statuses():
            if p.kind == "llm_backend" and p.started and p.healthy:
                models += [(f"{p.name}:{m}", f"{p.name} · {m}") for m in (await host.setting_choices("llm_backend", p.name)).get("model", [])]
        route = host.effective(kind, name).get("route") or {}
        now = f"{route['backend']}:{route['model']}" if route.get("backend") and route.get("model") else ""
        if now and now not in [v for v, _ in models]:
            models.insert(0, (now, now.replace(":", " · ", 1)))
        return {"route_models": models, "route_now": now}

    def _stage_choice(self, kind: str, name: str) -> dict[str, Any]:
        """The stages this kind of plugin can run in, and the one this instance is in: what its settings dialog offers as ``stage``."""
        stages = [(q.name, q.title) for q in self.svc.queues.all() if q.kind == kind]
        now = self._host().tables.table(kind, name).get("stage") or (stages[0][0] if stages else "")
        return {"stages": stages, "stage": now}

    async def _modals(self, plugins: list[Any]) -> dict[str, dict[str, Any]]:
        """What each row's settings dialog shows (the same data as the plugin's own page). A plugin whose
        settings cannot be read has none, and its dialog says so."""
        out: dict[str, dict[str, Any]] = {}
        for p in plugins:
            try:
                modal = await self._choices(p.kind, p.name, self._modal(p.kind, p.name))
                out[f"{p.kind}/{p.name}"] = {"sections": modal["sections"], "values": self._values(p.kind, p.name),
                                              "llm": self._host().llm_defaults(p.kind, p.name), "own": self._host().tables.table(p.kind, p.name),
                                              **self._stage_choice(p.kind, p.name), **await self._route_choice(p.kind, p.name)}  # fmt: skip
            except OakenError:
                continue
        return out

    async def stage(self, request: Request) -> Response:
        name = request.path_params["queue"]
        spec = next((q for q in self._stage_specs() if q.name == name), None)
        if spec is None:
            raise NotFound(f"no stage named {name!r}")
        stages, rows = await self._flow()
        jobs = (await self._op("job_list", {"queue": name, "limit": 50}))["jobs"]
        failed = (await self._op("job_list", {"queue": name, "status": "failed", "limit": 50}))["jobs"]
        first = next((q.name for q in self.svc.queues.all() if q.kind == spec.kind), None)
        every = [p for p in self._host().statuses() if p.kind == spec.kind]
        home = {p.name: self._host().tables.table(p.kind, p.name).get("stage") or first for p in every}  # the stage each runs in
        active = [p for p in every if p.enabled and home[p.name] == name and p.name not in spec.disabled]
        available = [p for p in every if p not in active]
        bases = self._bases(spec.kind)
        workers = [p for p in active if p.plugin != p.name] if bases else []  # the instances of a template: the Workers table
        active = [p for p in active if p not in workers]
        why = {p.name: ("a template: add an instance" if p.template else "off everywhere" if not p.enabled else f"runs in {home[p.name]}" if home[p.name] != name else "switched off in this stage") for p in available}
        return self.render("stage.html", stage=spec, bases=bases, workers=workers, active=active, available=available, why=why, stages=stages, jobs=[j for j in jobs if j["status"] != "failed"], failed=failed, row=rows.get(name, {"queued": 0, "running": 0, "failed": 0, "paused": False, "oldest_queued_seconds": None, "last_error": None}),
                           plugins=(plugins := [p for p in self._host().statuses() if p.kind == spec.kind]), modals=await self._modals(plugins))  # fmt: skip

    async def capabilities(self, request: Request) -> Response:
        """Every operation (the registry the CLI lists), by the part that offers it, with its MCP switch."""
        rows = (await self._op("exposure_list"))["operations"]
        kind_of = {s.name: s.kind for s in self._host().statuses()}
        by_group = {g: title for title, members in CAPABILITY_CATEGORIES for g in members}
        core: dict[str, list[dict[str, Any]]] = {}
        plugin: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            if r["plugin"]:
                plugin.setdefault(kind_of.get(r["plugin"], "plugin"), []).append(r)
            else:
                core.setdefault(by_group.get(r["group"] or "general", "Other"), []).append(r)
        order = [t for t, _ in CAPABILITY_CATEGORIES] + ["Other"]
        groups = [(f"Core: {t}", sorted(core[t], key=lambda o: o["name"])) for t in order if t in core] + [(f"{k} plugins", plugin[k]) for k in sorted(plugin)]
        return self.render("capabilities.html", groups=groups, power=self.svc.settings.api.mcp_enabled)

    async def expose(self, request: Request, form: dict[str, str]) -> Response:
        """A row's MCP switch. The click is the person's confirmation, so the call is confirmed."""
        await self.client.call("exposure_set", {"name": form.get("name", ""), "enabled": form.get("mcp") == "on"}, UI, confirm=True)
        return RedirectResponse("/capabilities", 303)

    async def mcp_power(self, request: Request, form: dict[str, str]) -> Response:
        await self.client.call("mcp_power", {"enabled": form.get("mcp") == "on"}, UI, confirm=True)
        return RedirectResponse("/capabilities", 303)

    @staticmethod
    def _kind_label(kind: str) -> str:
        """``llm_backend`` as ``LLM backends``: the words of the kind, in the plural, with LLM in capitals."""
        words = ["LLM" if w == "llm" else w for w in kind.split("_")]
        words[0] = words[0] if words[0] == "LLM" else words[0].capitalize()
        return " ".join(words) + ("" if words[-1].endswith("s") else "s")

    async def pipeline(self, request: Request) -> Response:
        """Pipeline > Machinery: the plugins that do not read an item's content (stores, models, embedders...): a collapsible
        section for each kind with something live, then one folded table of every disabled one."""
        return await self._plugin_tab("machinery", analyzers=False, skip=("provider",))

    async def providers(self, request: Request) -> Response:
        """Pipeline > Providers: the plugins that feed Intake (a mailbox, a socket, a file of JSON lines)."""
        return await self._plugin_tab("providers", analyzers=False, only=("provider",))

    async def analyzers(self, request: Request) -> Response:
        """Pipeline > Analyzers: the plugins that read the pipeline's content (triagers, enrichers, actions...), by kind, each
        with the stage that runs it."""
        return await self._plugin_tab("analyzers", analyzers=True)

    async def _plugin_tab(self, tab: str, *, analyzers: bool, only: tuple[str, ...] = (), skip: tuple[str, ...] = ()) -> Response:
        stage_of = {q.kind: q for q in self._stage_specs()}
        stages, _ = await self._flow()
        every = self._host().statuses()
        sections, empty, plugins = [], [], []
        for spec in self._host().kinds:
            if spec.analyzer != analyzers or (only and spec.name not in only) or spec.name in skip:
                continue
            found = [p for p in every if p.kind == spec.name]
            plugins += found
            live = [p for p in found if p.enabled]
            q = stage_of.get(spec.name)
            if live:
                sections.append((spec.name, self._kind_label(spec.name), live, (f"/stage/{q.name}", q.title) if q else None))
            elif not found:
                empty.append(self._kind_label(spec.name))
        bases = self._bases("provider") if tab == "providers" else []  # a provider that is a template (an account, a mailbox) is made into instances here: providers have no stage
        return self.render("pipeline.html", tab=tab, bases=bases, stages=stages, sections=sections, empty=empty, inactive=[p for p in plugins if not p.enabled],
                           modals=await self._modals(plugins))

    async def labels_page(self, request: Request) -> Response:
        """Pipeline, the default tab: the label keys that exist, and the owner's own to add, change or remove.
        Settings only: what labels subjects carry is content, and not for this console."""
        stages, _ = await self._flow()
        return self.render("labels.html", stages=stages, keys=(await self._op("label_keys"))["keys"])

    async def label_verb(self, request: Request, form: dict[str, str]) -> Response:
        """Add, change or remove one of the owner's label keys. Each shows what it will do first."""
        verb = request.path_params.get("verb", "new")
        key = request.path_params.get("key", form.get("key", "").strip())
        if verb not in ("new", "edit", "delete"):
            raise NotFound("no such action")
        values = [v.strip() for v in form.get("values", "").replace("\n", ",").split(",") if v.strip()]
        reserved = form.get("reserved") == "on"
        writers = [w.strip() for w in form.get("writers", "").replace("\n", ",").split(",") if w.strip()]
        payload = {"new": {"key": key, "description": form.get("description", ""), "values": values, "reserved": reserved, "writers": writers},
                   "edit": {"key": key, "description": form.get("description", ""), "values": values, "reserved": reserved, "writers": writers},
                   "delete": {"key": key}}[verb]  # fmt: skip
        out = await self._gated(f"label_{verb}", payload, form, request.url.path)
        if isinstance(out, Response):
            return out
        return self.render("result.html", title=f"{verb.capitalize()}: {out.key}", back="/pipeline/labels", result={"note": out.note})

    async def overview(self, request: Request) -> Response:
        stages, _ = await self._flow()
        plugins = self._host().statuses()
        return self.render("overview.html", status=await self._op("status"), plugins=plugins, stages=stages, modals=await self._modals(plugins))

    async def plugins(self, request: Request) -> Response:
        plugins = self._host().statuses()
        return self.render("plugins.html", plugins=plugins, modals=await self._modals(plugins))

    async def _plugin_stats(self, kind: str, name: str) -> dict[str, Any]:
        """Numbers for a plugin's page: its calls since this core started, a triager's verdicts (from the change log) and a
        model analyzer's usage and savings (from the router's ledger). Counts only, never text."""
        from oaken_core.plugins import stats as counters

        s = counters.get(kind, name)
        calls = {"calls": s.calls, "errors": s.errors, "avg_ms": round(s.avg_ms), "last_ago": None if s.last_at is None else max(0, int(time.time() - s.last_at))}
        verdicts: list[dict[str, Any]] = []
        if kind == "triager":
            rows = await self.svc.db.fetchall(
                "SELECT COALESCE(json_extract(new_value, '$.verdict'), op) AS verdict, COUNT(*) AS n, MAX(ts) AS last FROM change_log"
                " WHERE key = 'intake' AND actor_kind = 'system' AND actor = ? GROUP BY verdict ORDER BY n DESC", (name,))
            verdicts = [{"verdict": r["verdict"], "n": r["n"], "last": r["last"]} for r in rows]
        usage: list[Any] = []
        spent = 0
        if self.svc.router is not None:
            usage = [dataclasses.asdict(r) for r in await self.svc.router.usage(name)]
            spent = sum(r.tokens for r in await self.svc.router.spent_today(name))
        saved = sum(u["tokens_saved"] for u in usage)
        return {"calls": calls, "verdicts": verdicts, "usage": usage, "spent_today": spent, "saved": saved}

    async def plugin(self, request: Request) -> Response:
        kind, name = request.path_params["kind"], request.path_params["name"]
        modal = await self._choices(kind, name, self._modal(kind, name))
        values = self._values(kind, name)
        return self.render("plugin.html", status=self._status(kind, name), values=values, display=forms.display,
                           llm=self._host().llm_defaults(kind, name), own=self._host().tables.table(kind, name),
                           pages=modal["spec"].pages if modal["spec"] else (), buttons=modal["spec"].buttons if modal["spec"] else (),
                           stats=await self._plugin_stats(kind, name), docs=await self._plugin_docs(kind, name), **self._stage_choice(kind, name), **await self._route_choice(kind, name), **modal)  # fmt: skip

    async def queues(self, request: Request) -> Response:
        return self.render("queues.html", **await self._op("queue_list"))

    def _file_page(self, text: str | None = None, flash: tuple[str, str] | None = None) -> Response:
        """The whole-file editor. ``text`` is what the person typed, kept when a check fails."""
        exists = self.config_path.is_file()
        if text is None:
            try:
                text = self.config_path.read_text(encoding="utf-8") if exists else ""
            except OSError as exc:
                raise ConfigError(f"cannot read {self.config_path}: {exc}") from exc
        return self.render("config_file.html", text=text, path=str(self.config_path), exists=exists, flash=flash)

    async def config_file(self, request: Request) -> Response:
        return self._file_page()

    async def config_file_test(self, request: Request, form: dict[str, str]) -> Response:
        text = form.get("text", "")
        out = await self._op("config_validate", {"text": text})
        if not out["ok"]:
            return self._file_page(text, ("danger", f"Invalid: {out['error']}"))
        return self._file_page(text, ("warn", _unfit(out)) if out["warnings"] else ("ok", "Valid. Safe to save."))

    async def config_file_save(self, request: Request, form: dict[str, str]) -> Response:
        """The Save button is the person's confirmation, so the call is confirmed. A file core would not
        start on is refused and the typed text stays on the page."""
        text = form.get("text", "")
        out = await self._op("config_validate", {"text": text})
        if not out["ok"]:
            return self._file_page(text, ("danger", f"Not saved: {out['error']}"))
        saved = (await self.client.call("config_save", {"text": text}, UI, confirm=True)).output
        note = "Saved. Reload to apply it." + (f" The old file is {saved.backup}." if saved and saved.backup else "")  # type: ignore[attr-defined]
        return self._file_page(text, ("warn", f"{note} {_unfit(out)}") if out["warnings"] else ("ok", note))

    async def config_file_reload(self, request: Request, form: dict[str, str]) -> Response:
        out = (await self.client.call("core_reload", {}, UI, confirm=True)).output
        return self.render("result.html", title="Reloading", back="/config", result={"note": out.note})  # type: ignore[union-attr]

    def _page(self, kind: str, name: str, page: str) -> UiPage:
        spec = self._host().plugin_ui(kind, name)
        for p in spec.pages if spec else ():
            if p.name == page:
                return p
        raise NotFound(f"{name} has no page {page!r}")

    async def plugin_page(self, request: Request) -> Response:
        kind, name, page = (request.path_params[k] for k in ("kind", "name", "page"))
        p = self._page(kind, name, page)
        loaded = await self._op(f"{name}{PLUGIN_SEP}{p.load}")
        return self.render("page.html", kind=kind, name=name, p=p, fields=[forms.from_ui(f) for f in p.fields],
                           values=loaded.get("values") or {}, messages=loaded.get("messages") or [],
                           rows=loaded.get("rows") or [], display=forms.display, result=None)  # fmt: skip

    # -- json ----------------------------------------------------------------------------------
    async def j_status(self, request: Request) -> Any:
        return await self._op("status")

    async def j_plugins(self, request: Request) -> Any:
        return [dataclasses.asdict(s) for s in self._host().statuses()]

    async def j_ui(self, request: Request) -> Any:
        """The plugin's UI as data. ``source`` says which: its own spec, or the schema fallback."""
        kind, name = request.path_params["kind"], request.path_params["name"]
        self._status(kind, name)
        modal = self._modal(kind, name)
        spec = modal["spec"]
        return {"plugin": name, "kind": kind, "source": "ui" if spec else "schema",
                "spec": dataclasses.asdict(spec) if spec else None, "schema": modal["schema"],
                "values": self._values(kind, name)}  # fmt: skip

    async def j_queues(self, request: Request) -> Any:
        return await self._op("queue_list")

    async def j_config(self, request: Request) -> Any:
        return self.svc.settings.model_dump(mode="json")

    # -- mutations -----------------------------------------------------------------------------
    async def _gated(self, op: str, payload: Any, form: dict[str, str], back: str) -> Response | Any:
        """Run ``op`` for the person. A ``mutate`` that is not confirmed yet shows what it would
        change, with every field of the form carried along so a second click repeats the call.
        ``back`` is where the confirm form posts: always the route handling this request (``request.url.path``),
        never a page, because a page only answers GET."""
        res = await self.client.call(op, payload, UI, confirm=form.get("confirm") == "1")
        if res.proposal is not None:
            carried = {k: v for k, v in form.items() if k != "confirm"}
            return self.render("confirm.html", proposal=res.proposal, carried=carried, back=back)
        return res.output

    async def plugin_verb(self, request: Request, form: dict[str, str]) -> Response:
        kind, name, verb = (request.path_params[k] for k in ("kind", "name", "verb"))
        if verb not in ("enable", "disable", "reload"):
            raise NotFound("no such action")
        self._status(kind, name)
        await self.client.call(f"plugin_{verb}", {"name": name}, UI, confirm=True)  # the click is the confirmation
        return RedirectResponse(self._back("/plugins"), 303)

    async def press(self, request: Request, form: dict[str, str]) -> Response:
        """A button of the plugin's spec: one of its own operations, taking no input."""
        kind, name = request.path_params["kind"], request.path_params["name"]
        spec = self._host().plugin_ui(kind, name)
        op = form.get("operation", "")
        if spec is None or op not in {b.operation for b in spec.buttons}:
            raise PermissionDenied(f"{name} has no button for {op!r}")
        out = await self._gated(f"{name}{PLUGIN_SEP}{op}", {}, form, request.url.path)
        if isinstance(out, Response):
            return out
        return self.render("result.html", title=op, back=f"/plugins/{kind}/{name}", result=out.model_dump(mode="json") if out else {})

    async def submit_page(self, request: Request, form: dict[str, str]) -> Response:
        kind, name, page, action = (request.path_params[k] for k in ("kind", "name", "page", "action"))
        p = self._page(kind, name, page)
        op = {"test": p.test, "submit": p.submit, "order": p.order}.get(action, "")
        if not op:
            raise NotFound(f"page {page} has no {action}")
        if action == "order":
            ids = form.get("ids", "").split(",")
            payload: Any = {"order": reposition(ids, {i: form.get(f"pos_{i}") for i in ids})}
        else:
            payload = forms.parse([forms.from_ui(f) for f in p.fields], form)
        back = f"/plugins/{kind}/{name}/pages/{page}"
        out = await self._gated(f"{name}{PLUGIN_SEP}{op}", payload, form, request.url.path)
        if isinstance(out, Response):
            return out
        result = out.model_dump(mode="json") if out else {}
        if action == "test":  # keep the form (and a pasted payload) in front of the person, result below it
            loaded = await self._op(f"{name}{PLUGIN_SEP}{p.load}")
            return self.render("page.html", kind=kind, name=name, p=p, fields=[forms.from_ui(f) for f in p.fields], values=payload, messages=[],
                               rows=loaded.get("rows") or [], display=forms.display, result=result)  # fmt: skip
        return self.render("result.html", title=p.title, back=back, result=result)

    async def add_queue(self, request: Request, form: dict[str, str]) -> Response:
        """The "Add a stage" form: a queue for one analyzer kind. A change, so it shows what it will do first."""
        title = form.get("label", "").strip()
        payload = {"name": form.get("name", "").strip() or title, "kind": form.get("kind", ""), "label": title,
                   "workers": int(form.get("workers") or 1)}  # fmt: skip
        out = await self._gated("queue_new", payload, form, request.url.path)
        if isinstance(out, Response):
            return out
        return self.render("result.html", title=f"Added {out.name}", back="/", result={"note": out.note})

    async def stage_plugin(self, request: Request, form: dict[str, str]) -> Response:
        """A stage page's switch: this plugin on or off in this stage only (the stage's ``disabled`` list). The plugin itself
        stays on, and still runs in any other stage. The click is the confirmation."""
        queue, name, state = request.path_params["queue"], request.path_params["name"], request.path_params["state"]
        if state not in ("on", "off"):
            raise NotFound("no such action")
        stage = self.svc.queues.get(queue)
        if state == "on":  # whatever keeps it out of this stage, undone: off everywhere, run in another stage, off here
            kind = stage.kind
            if not self._status(kind, name).enabled:
                await self.client.call("plugin_enable", {"name": name}, UI, confirm=True)
            first = next((q.name for q in self.svc.queues.all() if q.kind == kind), None)
            if (self._host().tables.table(kind, name).get("stage") or first) != queue:
                have = get_table(self.config_path.read_text(encoding="utf-8") if self.config_path.exists() else "", ["plugins", kind, name])
                save_table(self.config_path, ["plugins", kind, name], {**have, "stage": queue})
                self._host().tables.tables.setdefault(kind, {}).setdefault(name, {})["stage"] = queue
        off = [n for n in stage.disabled if n != name] + ([name] if state == "off" else [])
        if set(off) != set(stage.disabled):
            await self.client.call("queue_edit", {"queue": queue, "disabled": off}, UI, confirm=True)
        return RedirectResponse(self._back(f"/stage/{queue}"), 303)

    async def job_requeue(self, request: Request, form: dict[str, str]) -> Response:
        """A failed job's Requeue button: the click is the confirmation (it runs again in its own queue)."""
        await self.client.call("queue_requeue", {"job_id": request.path_params["job"]}, UI, confirm=True)
        return RedirectResponse(self._back("/"), 303)

    async def queue_verb(self, request: Request, form: dict[str, str]) -> Response:
        """A queue's own controls on its page: Start and Stop act on the click; edit, rename and delete show what they will do first."""
        name, verb = request.path_params["queue"], request.path_params["verb"]
        if verb not in ("pause", "resume", "edit", "rename", "delete"):
            raise NotFound("no such action")
        self.svc.queues.get(name)
        if verb in ("pause", "resume"):
            await self.client.call(f"queue_{verb}", {"queue": name}, UI, confirm=True)  # the click is the confirmation
            return RedirectResponse(self._back(f"/stage/{name}"), 303)
        payload = {"edit": {"queue": name, "label": form.get("label", "").strip(), "description": form.get("description", "").strip(),
                            "workers": int(form.get("workers") or 1)},
                   "rename": {"queue": name, "to": form.get("to", "")}, "delete": {"queue": name}}[verb]  # fmt: skip
        out = await self._gated(f"queue_{verb}", payload, form, request.url.path)
        if isinstance(out, Response):
            return out
        return self.render("result.html", title=f"{verb.capitalize()}: {out.name}", back="/" if verb != "edit" else f"/stage/{name}", result={"note": out.note})

    async def set_look(self, request: Request, form: dict[str, str]) -> Response:
        """The theme menu: palette and mode, kept in ``[console]`` of ``config.toml`` (the one
        thing the browser never stores)."""
        look = {k: form.get(k, self.look[k]) for k in self.look}
        if look["palette"] not in PALETTES or look["mode"] not in MODES:
            raise InvalidInput("unknown palette or mode")
        try:
            text = self.config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        except OSError as exc:
            raise ConfigError(f"cannot read {self.config_path}: {exc}") from exc
        table = {**tomllib.loads(text).get("console", {}), **look}
        tmp = self.config_path.with_name(self.config_path.name + f".{os.getpid()}.tmp")
        tmp.write_text(set_table(text, ["console"], table), encoding="utf-8")
        os.replace(tmp, self.config_path)
        self.look = look
        back = _here.get()
        return RedirectResponse(back if back.startswith("/") and not back.startswith("//") else "/", 303)

    def _instructions(self, kind: str, name: str, form: dict[str, str]) -> dict[str, str]:
        """The ``prompt`` and ``guard`` the form asks for. They are the owner's only if the unlock box is
        ticked (and the box is not empty); otherwise the plugin's own apply. A plugin that does not
        ``needs_llm`` has none, whatever the form says."""
        if self._host().llm_defaults(kind, name) is None or "unlock" not in form:
            return {}
        return {k: form[k].strip() for k in ("prompt", "guard") if form.get(k, "").strip()}

    def _host_part(self, kind: str, name: str, form: dict[str, str]) -> dict[str, Any]:
        """The host's keys as saved, with ``prompt`` and ``guard`` taken from the form."""
        keep = {k: v for k, v in self._host().tables.table(kind, name).items() if k in HOST_KEYS and k not in ("prompt", "guard")}
        picked_model = form.get("route_model", "")
        if ":" in picked_model:  # the model dropdown: backend and model, over whatever else the route says
            backend, _, model = picked_model.partition(":")
            route = dict(self._host().effective(kind, name).get("route") or {})
            if (route.get("backend"), route.get("model")) != (backend, model):
                keep = {k: v for k, v in keep.items() if k != "route"} | {"route": route | {"backend": backend, "model": model}}
        choice = self._stage_choice(kind, name)
        picked = form.get("stage", "")
        if picked in [n for n, _ in choice["stages"]]:  # the stage this instance runs in; the first is the default, so it is not written
            keep = {k: v for k, v in keep.items() if k != "stage"} | ({} if picked == choice["stages"][0][0] else {"stage": picked})
        return keep | self._instructions(kind, name, form)

    async def try_settings(self, request: Request, form: dict[str, str]) -> Response:
        """The settings modal's Test: the form's values on a throwaway copy of the plugin. Nothing is
        saved and the running plugin is not touched, so there is nothing to confirm."""
        kind, name = request.path_params["kind"], request.path_params["name"]
        self._status(kind, name)
        modal = self._modal(kind, name)
        table = self._host_part(kind, name, form) | forms.parse(modal["fields"], form)
        out = await self._op("plugin_test", {"kind": kind, "name": name, "config": table})
        result = {"works": out["ok"], "detail": out["detail"] or ("started and reported healthy" if out["ok"] else "")}
        own = {k: form[k] for k in ("prompt", "guard") if form.get("unlock") and k in form}
        token = secrets.token_urlsafe(8)
        self._tests[token] = {"key": f"{kind}/{name}", "result": result, "values": forms.parse(modal["fields"], form), "own": own}
        while len(self._tests) > 16:  # a result is only for the page it comes back to
            del self._tests[next(iter(self._tests))]
        back = self._back(f"/plugins/{kind}/{name}")
        return RedirectResponse(f"{back}{'&' if '?' in back else '?'}test={token}", 303)  # the dialog reopens with the result and what was typed

    async def new_instance(self, request: Request, form: dict[str, str]) -> Response:
        """Another named instance of this plugin, with settings of its own (a second ``llm`` with another model, in another
        stage). The same ``plugin_add`` the command line uses: it shows what it will write, then does it."""
        kind, name = request.path_params["kind"], request.path_params["name"]
        status = self._status(kind, name)
        config = {"stage": form["stage"]} if form.get("stage") in [n for n, _ in self._stage_choice(kind, name)["stages"]] else {}
        must = set(self._host().contextual(kind, status.plugin if status.plugin == name else name))
        config |= forms.parse([f for f in self._modal(kind, name)["fields"] if f.name in must], form)  # what makes it an instance
        out = await self._gated("plugin_add", {"plugin": status.plugin, "kind": kind, "name": form.get("instance_name", "").strip(), "config": config}, form, request.url.path)
        if isinstance(out, Response):
            return out
        return self.render("result.html", title=f"Added {out.name}", back=f"/plugins/{kind}/{out.name}", result={"note": out.detail or ("started" if out.started else "")})

    async def remove_instance(self, request: Request, form: dict[str, str]) -> Response:
        """Delete a named instance: the same ``plugin_remove`` the API has, which shows what it will do first."""
        kind, name = request.path_params["kind"], request.path_params["name"]
        self._status(kind, name)
        out = await self._gated("plugin_remove", {"kind": kind, "name": name}, form, request.url.path)
        if isinstance(out, Response):
            return out
        return self.render("result.html", title=f"Removed {out.name}", back=self._back("/"), result={"note": "stopped and deleted from config.toml"})

    async def save_settings(self, request: Request, form: dict[str, str]) -> Response:
        """Write the plugin's table to ``config.toml`` and restart that plugin on it. Checked
        against its ``config_model`` first, and never a secret. Only the plugin is touched: core and
        the console keep running whatever the result."""
        kind, name = request.path_params["kind"], request.path_params["name"]
        self._status(kind, name)
        host = self._host()
        modal = self._modal(kind, name)
        if not modal["fields"] and host.llm_defaults(kind, name) is None and not self._stage_choice(kind, name)["stages"]:
            raise InvalidInput(f"{name} declares no settings to edit")
        values = forms.parse(modal["fields"], form)
        refuse_secrets(values)
        inherited = host.inherited(kind, name)
        values = {k: v for k, v in values.items() if k not in inherited or inherited[k] != v}  # an instance keeps only what it overrides
        table = self._host_part(kind, name, form) | values
        problems = host.check_config(name, table)
        if problems:
            raise InvalidInput("; ".join(problems))
        try:
            text = self.config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        except OSError as exc:
            raise ConfigError(f"cannot read {self.config_path}: {exc}") from exc
        updated = set_table(text, ["plugins", kind, name], table)
        tmp = self.config_path.with_name(self.config_path.name + f".{os.getpid()}.tmp")
        tmp.write_text(updated, encoding="utf-8")
        os.replace(tmp, self.config_path)
        host.tables.tables.setdefault(kind, {})[name] = table
        await host.reload(name)
        log.info("plugin settings saved from the console", kind=kind, name=name, keys=sorted(values))
        return RedirectResponse(self._back(f"/plugins/{kind}/{name}"), 303)
