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
import hashlib
import hmac
import os
import secrets
import tomllib
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import quote

import jinja2
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from oaken_core import identity, paths
from oaken_core.api.errors import status_for
from oaken_core.config_edit import refuse_secrets, set_table
from oaken_core.console import forms
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
CSS_VERSION = hashlib.sha256((HERE / "static" / "oaken.css").read_bytes()).hexdigest()[:10]
_here: ContextVar[str] = ContextVar("console_here", default="/")  # the page being drawn, for nav and "back"


Handler = Callable[[Request], Awaitable[Response]]


# One token for the life of the process, so a page left open across a core reload still posts.
_PROCESS_TOKEN = secrets.token_urlsafe(32)


class Console:
    """An ASGI app. ``bind(port)`` is called once the socket is open (the Host check needs it)."""

    def __init__(self, client: CoreClient, svc: Services) -> None:
        self.client, self.svc = client, svc
        self.token = _PROCESS_TOKEN
        self.look = {"palette": svc.settings.console.palette, "mode": svc.settings.console.mode}
        self.port = PortRef()
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True, undefined=jinja2.StrictUndefined
        )
        self.env.globals["display"] = forms.display  # macros do not see the page's context
        self.config_path = paths.config_dir() / "config.toml"
        routes = [
            Route("/", self.page(self.overview)),
            Route("/plugins", self.page(self.plugins)),
            Route("/pipeline", self.page(self.labels_page)),
            Route("/pipeline/plugins", self.page(self.pipeline)),
            Route("/ui/labels/{verb}", self.post(self.label_verb), methods=["POST"]),
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
            Route("/ui/expose", self.post(self.expose), methods=["POST"]),
            Route("/ui/mcp-power", self.post(self.mcp_power), methods=["POST"]),
            Route("/ui/config-file/test", self.post(self.config_file_test), methods=["POST"]),
            Route("/ui/config-file/save", self.post(self.config_file_save), methods=["POST"]),
            Route("/ui/config-file/reload", self.post(self.config_file_reload), methods=["POST"]),
            Route("/ui/plugins/{kind}/{name}/settings", self.post(self.save_settings), methods=["POST"]),
            Route("/ui/plugins/{kind}/{name}/try", self.post(self.try_settings), methods=["POST"]),
            Route("/ui/plugins/{kind}/{name}/button", self.post(self.press), methods=["POST"]),
            Route("/ui/plugins/{kind}/{name}/pages/{page}/{action}", self.post(self.submit_page), methods=["POST"]),
            Route("/ui/plugins/{kind}/{name}/{verb}", self.post(self.plugin_verb), methods=["POST"]),
            Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
        ]
        self.app = ConsoleGuard(Starlette(routes=routes), self.port)

    def bind(self, port: int) -> None:
        self.port.value = port

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self.app(scope, receive, send)

    # -- plumbing ------------------------------------------------------------------------------
    def _addable(self) -> list[str]:
        """The analyzer kinds that have no queue yet, for the Add a stage button; none when a product fixes its own."""
        if not self.svc.queues_from_config or self.svc.host is None:
            return []
        served = {q.kind for q in self._stage_specs()}
        return [k.name for k in self.svc.host.kinds if k.analyzer and k.name not in served]

    def render(self, template: str, http: int = 200, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("addable", self._addable())
        html = self.env.get_template(template).render(
            nav=self._nav(), app_title=identity.title, wordmark=identity.wordmark(), token=self.token, csrf=self.token, version=self.svc.version, look=self.look, palettes=PALETTES,
            modes=MODES, css_version=CSS_VERSION, here=_here.get(), **ctx)
        return HTMLResponse(html, status_code=http)

    def fail(self, exc: BaseException) -> Response:
        if isinstance(exc, OakenError):
            return self.render("error.html", status_for(exc), code=exc.code, message=exc.message)
        log.error("console handler failed", error=type(exc).__name__)
        return self.render("error.html", 500, code="internal", message="something went wrong inside core; see its log")

    def page(self, fn: Handler) -> Handler:
        async def wrapped(request: Request) -> Response:
            _here.set(request.url.path)
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
        return {k: v for k, v in self._host().tables.table(kind, name).items() if k not in HOST_KEYS}

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
            if q.kind or q.name == DEFAULT:
                r = rows.get(q.name, {})
                out.append({"name": q.name, "title": "Intake" if q.name == DEFAULT else q.title,
                            "href": "/" if q.name == DEFAULT else f"/stage/{q.name}", "queued": r.get("queued", 0),
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

    async def _modals(self, plugins: list[Any]) -> dict[str, dict[str, Any]]:
        """What each row's settings dialog shows (the same data as the plugin's own page). A plugin whose
        settings cannot be read has none, and its dialog says so."""
        out: dict[str, dict[str, Any]] = {}
        for p in plugins:
            try:
                modal = await self._choices(p.kind, p.name, self._modal(p.kind, p.name))
                out[f"{p.kind}/{p.name}"] = {"sections": modal["sections"], "values": self._values(p.kind, p.name),
                                              "llm": self._host().llm_defaults(p.kind, p.name), "own": self._host().tables.table(p.kind, p.name)}  # fmt: skip
            except OakenError:
                continue
        return out

    async def stage(self, request: Request) -> Response:
        name = request.path_params["queue"]
        spec = next((q for q in self._stage_specs() if q.name == name), None)
        if spec is None:
            raise NotFound(f"no stage named {name!r}")
        stages, rows = await self._flow()
        return self.render("stage.html", stage=spec, stages=stages, row=rows.get(name, {"queued": 0, "running": 0, "failed": 0, "paused": False, "oldest_queued_seconds": None, "last_error": None}),
                           plugins=(plugins := [p for p in self._host().statuses() if p.kind == spec.kind]), modals=await self._modals(plugins))  # fmt: skip

    async def capabilities(self, request: Request) -> Response:
        """Every operation (the registry the CLI lists), by the part that offers it, with its MCP switch."""
        rows = (await self._op("exposure_list"))["operations"]
        kind_of = {s.name: s.kind for s in self._host().statuses()}
        core: dict[str, list[dict[str, Any]]] = {}
        plugin: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            if r["plugin"]:
                plugin.setdefault(kind_of.get(r["plugin"], "plugin"), []).append(r)
            else:
                core.setdefault(r["group"] or "general", []).append(r)
        groups = [(f"Core: {g}", core[g]) for g in sorted(core)] + [(f"{k} plugins", plugin[k]) for k in sorted(plugin)]
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
        """The machinery: a collapsible section for each kind of plugin that is not a stage's (stores, providers, models,
        embedders...) that has something in it. A stage's own plugins are on its page."""
        stage_kinds = {q.kind for q in self._stage_specs()}
        stages, _ = await self._flow()
        every = self._host().statuses()
        sections, empty = [], []
        for k in (k.name for k in self._host().kinds if k.name not in stage_kinds):
            found = [p for p in every if p.kind == k]
            (sections if found else empty).append((k, self._kind_label(k), found) if found else self._kind_label(k))
        plugins = [p for _, _, ps in sections for p in ps]
        return self.render("pipeline.html", stages=stages, sections=sections, empty=empty, modals=await self._modals(plugins))

    async def labels_page(self, request: Request) -> Response:
        """Pipeline, the default tab: the keys that exist, then a look-up by subject or by key, with set and clear beside the result."""
        stages, _ = await self._flow()
        q = request.query_params
        subject, key, value = q.get("subject", "").strip(), q.get("key", "").strip(), q.get("value", "").strip()
        keys = (await self._op("label_keys"))["keys"]
        got = (await self._op("label_get", {"subject_id": subject}))["labels"] if subject else None
        found = (await self._op("label_find", {"key": key, "value": value or None}))["hits"] if key else None
        return self.render("labels.html", stages=stages, keys=keys, subject=subject, key=key, value=value, got=got, found=found)

    async def label_verb(self, request: Request, form: dict[str, str]) -> Response:
        """Set or clear one label for a subject, as a person (the human layer). Shows what it will do first."""
        verb = request.path_params["verb"]
        if verb not in ("set", "clear"):
            raise NotFound("no such action")
        payload = {"subject_id": form.get("subject", "").strip(), "key": form.get("key", "").strip()}
        if verb == "set":
            payload["value"] = form.get("value", "")
        out = await self._gated(f"label_{verb}", payload, form, request.url.path)
        if isinstance(out, Response):
            return out
        return RedirectResponse(f"/pipeline?subject={quote(payload['subject_id'])}", 303)

    async def overview(self, request: Request) -> Response:
        stages, _ = await self._flow()
        plugins = self._host().statuses()
        return self.render("overview.html", status=await self._op("status"), plugins=plugins, stages=stages, modals=await self._modals(plugins))

    async def plugins(self, request: Request) -> Response:
        plugins = self._host().statuses()
        return self.render("plugins.html", plugins=plugins, modals=await self._modals(plugins))

    async def plugin(self, request: Request) -> Response:
        kind, name = request.path_params["kind"], request.path_params["name"]
        modal = await self._choices(kind, name, self._modal(kind, name))
        values = self._values(kind, name)
        return self.render("plugin.html", status=self._status(kind, name), values=values, display=forms.display,
                           llm=self._host().llm_defaults(kind, name), own=self._host().tables.table(kind, name),
                           pages=modal["spec"].pages if modal["spec"] else (), buttons=modal["spec"].buttons if modal["spec"] else (),
                           **modal)  # fmt: skip

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
        return self._file_page(text, ("ok", "Valid. Safe to save.") if out["ok"] else ("danger", f"Invalid: {out['error']}"))

    async def config_file_save(self, request: Request, form: dict[str, str]) -> Response:
        """The Save button is the person's confirmation, so the call is confirmed. A file core would not
        start on is refused and the typed text stays on the page."""
        text = form.get("text", "")
        out = await self._op("config_validate", {"text": text})
        if not out["ok"]:
            return self._file_page(text, ("danger", f"Not saved: {out['error']}"))
        saved = (await self.client.call("config_save", {"text": text}, UI, confirm=True)).output
        note = "Saved. Reload to apply it." + (f" The old file is {saved.backup}." if saved and saved.backup else "")  # type: ignore[attr-defined]
        return self._file_page(text, ("ok", note))

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
        return self.render("result.html", title=p.title, back=back, result=out.model_dump(mode="json") if out else {})

    async def add_queue(self, request: Request, form: dict[str, str]) -> Response:
        """The "Add a stage" form: a queue for one analyzer kind. A change, so it shows what it will do first."""
        title = form.get("label", "").strip()
        payload = {"name": form.get("name", "").strip() or title, "kind": form.get("kind", ""), "label": title,
                   "workers": int(form.get("workers") or 1)}  # fmt: skip
        out = await self._gated("queue_new", payload, form, request.url.path)
        if isinstance(out, Response):
            return out
        return self.render("result.html", title=f"Added {out.name}", back="/", result={"note": out.note})

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
        return self.render("result.html", title=f"Test {name}", back=self._back(f"/plugins/{kind}/{name}"), result=result)

    async def save_settings(self, request: Request, form: dict[str, str]) -> Response:
        """Write the plugin's table to ``config.toml`` and restart that plugin on it. Checked
        against its ``config_model`` first, and never a secret. Only the plugin is touched: core and
        the console keep running whatever the result."""
        kind, name = request.path_params["kind"], request.path_params["name"]
        self._status(kind, name)
        host = self._host()
        modal = self._modal(kind, name)
        if not modal["fields"] and host.llm_defaults(kind, name) is None:
            raise InvalidInput(f"{name} declares no settings to edit")
        values = forms.parse(modal["fields"], form)
        refuse_secrets(values)
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
