# SPDX-License-Identifier: Apache-2.0
"""Optional base classes that make writing a plugin short.

Adapted from oaken-mail ``plugins/base.py`` (cf3b187): ``BasePlugin`` and the ``@command``
decorator. Nothing requires these: any object with the members of a contract works, and the host
checks the same rules either way (``oaken_core.plugins.validate``).

    class Hello(BasePlugin):
        @command("hi", "Say hi.", input=HiIn, output=HiOut)
        async def hi(self, ctx: PluginOpContext, inp: HiIn) -> HiOut: ...

A ``@command`` becomes an operation ``<plugin>.<name>``: it is in the operation registry, so the
CLI builds a verb for it (``<product> <plugin> <name>``, one flag per input field) at the moment the
host loads the plugin, and a plugin that fails to load takes its commands with it.
"""

import functools
import inspect
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeVar

from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from oaken_core.db import Database
from oaken_core.errors import Deferred, PluginError
from oaken_core.labels import BaseLabel
from oaken_core.plugins import stats
from oaken_core.trace import brief, trace_context
from oaken_core.plugins.contracts import (
    PLUGIN_API_VERSION,
    CacheEntry,
    CacheStats,
    Health,
    Hit,
    LLMRequest,
    LLMResponse,
    PluginContext,
    PluginJobHandler,
    PluginOperation,
    PluginScheduledTask,
    UiSpec,
    VectorItem,
)

if TYPE_CHECKING:
    from oaken_core.router.router import Route

F = TypeVar("F", bound=Callable[..., Any])
M = TypeVar("M", bound=BaseModel)


def _default(fn: F) -> F:
    """Mark a base-class method as a placeholder: overriding it is what implements it."""
    fn._oaken_default = True  # type: ignore[attr-defined]
    return fn


# The calls a plugin author implements and core makes: what is asked of it, and what it answers. Every one a
# plugin class defines is traced by ``BasePlugin`` with no code in the plugin (see ``_traced``).
TRACED = ("generate", "embed", "upsert", "delete", "query", "get", "put", "clear", "triage", "enrich", "act", "ingest", "read", "split")
_TRACE_KIND = {"generate": "llm.call"}  # the kind the trace file already knows a model call by


def _shape(method: str, result: Any) -> Any:
    """What the trace keeps of an answer. A vector is not read by a person: a count and a size will do."""
    if method == "embed" and isinstance(result, list):
        return {"vectors": len(result), "dimensions": len(result[0]) if result else 0}
    return brief(result)


def _traced(method: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """``fn`` with its call recorded in the trace when tracing is on: who was asked (plugin kind and name), what,
    what came back or which error type, and how long it took. Off, it adds one attribute read and a call."""

    @functools.wraps(fn)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        ctx = getattr(self, "ctx", None)
        if ctx is None or not ctx.tracing():
            return await fn(self, *args, **kwargs)
        kind = _TRACE_KIND.get(method, f"{ctx.kind}.{method}")
        asked = {"args": brief(list(args)), **({"kwargs": brief(kwargs)} if kwargs else {})}
        started = time.monotonic()
        with trace_context(plugin_kind=ctx.kind, plugin=ctx.name):
            try:
                result = await fn(self, *args, **kwargs)
            except BaseException as exc:
                ctx.trace(kind, **asked, error=type(exc).__name__, ms=round((time.monotonic() - started) * 1000))
                raise
            ctx.trace(kind, **asked, result=_shape(method, result), ms=round((time.monotonic() - started) * 1000))
            return result

    @functools.wraps(fn)
    async def counted(self: Any, *args: Any, **kwargs: Any) -> Any:
        ctx = getattr(self, "ctx", None)
        if ctx is None:
            return await wrapper(self, *args, **kwargs)
        started, failed = time.monotonic(), False
        try:
            return await wrapper(self, *args, **kwargs)
        except Deferred:  # waiting for a backend is not a failure
            raise
        except Exception:
            failed = True
            raise
        finally:
            stats.record(ctx.kind, ctx.name, (time.monotonic() - started) * 1000, failed)

    counted._oaken_traced = True  # type: ignore[attr-defined]
    return counted


def is_implemented(obj: object, method: str) -> bool:
    """True if ``obj`` has a callable ``method`` that is not a base-class placeholder."""
    fn = getattr(obj, method, None)
    return callable(fn) and not getattr(fn, "_oaken_default", False)


def command(
    name: str,
    summary: str,
    *,
    input: type[BaseModel],
    output: type[BaseModel],
    permission: Literal["read", "mutate"] = "read",
    preview: str | None = None,
    mcp: Literal["off", "never"] = "off",
) -> Callable[[F], F]:
    """Offer a method of your plugin as a command: an operation core registers as
    ``<plugin>.<name>``. ``preview`` names another method (same signature, returning
    ``PluginPreview``) that says what a ``mutate`` command would change. A command is never on over
    MCP until the user turns it on."""

    def wrap(fn: F) -> F:
        fn._oaken_command = {  # type: ignore[attr-defined]
            "name": name, "summary": summary, "input": input, "output": output,
            "permission": permission, "preview": preview, "mcp": mcp,
        }  # fmt: skip
        return fn

    return wrap


class BasePlugin:
    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Wrap the calls this class implements (``TRACED``) so they appear in the trace. A placeholder a base class
        leaves for its subclasses is left alone, and so is a method that is already wrapped."""
        super().__init_subclass__(**kwargs)
        for name in TRACED:
            fn = cls.__dict__.get(name)
            if inspect.iscoroutinefunction(fn) and not getattr(fn, "_oaken_default", False) and not getattr(fn, "_oaken_traced", False):
                setattr(cls, name, _traced(name, fn))

    # The plugin kind this base is for (``triager``, ``llm_backend``...): how ``plugin add <class path>`` knows
    # where a class belongs without being told. Empty on the base of all of them.
    kind: ClassVar[str] = ""
    api_version: ClassVar[int] = PLUGIN_API_VERSION
    version: ClassVar[str] = ""
    # A version range of Oaken Core this plugin works with (``">=0.1,<0.2"``). Empty: any. The
    # host refuses a plugin outside it.
    requires_core: ClassVar[str] = ""
    # Shown where people set the plugin up: a few plain sentences and a config snippet.
    setup_help: ClassVar[str] = ""
    config_example: ClassVar[str] = ""
    # A pydantic model of this plugin's settings. Declare it and ``start`` parses the table into
    # ``self.config`` (typed, validated), and the host publishes its JSON Schema so the console can
    # render a settings form. Leave it None and the plugin reads ``ctx.config`` (a dict) itself.
    config_model: ClassVar[type[BaseModel] | None] = None
    # Whether the plugin runs when its table does not say ``enabled``. A plugin that should be
    # opt-in (it costs money, or reaches a network) sets this False.
    enabled_default: ClassVar[bool] = True
    # A fallback is used only while no other plugin of its kind is installed: a kind with one active picks the
    # only non-fallback one, so a product's plugin replaces core's default without any [active] setting.
    fallback: ClassVar[bool] = False
    # Settings the plugin cannot run without. While its table lacks any, the host leaves it
    # switched off with the reason shown, and starts it when they are set.
    requires_config: ClassVar[tuple[str, ...]] = ()
    # A folder of ``NNNN_name.sql`` migrations for tables the plugin owns, applied by the host (in
    # the same run as core's) before ``start``. Name the tables ``<plugin>_...``.
    migrations_dir: ClassVar[Path | None] = None
    # For an analyzer: the stored results it reads, ``"<analyzer>@<relation>"``. Must be acyclic.
    needs: ClassVar[tuple[str, ...]] = ()

    ctx: PluginContext  # set by start()
    config: Any = None  # the parsed config_model instance, set by start() when config_model is set

    def labels(self) -> list[BaseLabel]:
        """The label keys this plugin defines. Registered before ``start``; a clash with a
        reserved key raises ``LabelError`` and the plugin is switched off."""
        return []

    def ui(self) -> UiSpec | None:
        """The plugin's UI as data: settings sections, buttons and pages. None: the settings
        modal is built from the config schema alone."""
        return None

    def job_handlers(self) -> list[PluginJobHandler]:
        return []

    def scheduled_tasks(self) -> list[PluginScheduledTask]:
        return []

    async def setting_choices(self, config: dict[str, Any]) -> dict[str, list[str]]:
        """Values worth offering for some of this plugin's settings, by setting name, worked out from ``config`` (the
        table as it stands, which may not be enough to start on). A setting that gets choices is drawn as a dropdown in
        the settings form, with whatever is saved kept even if it is not offered. Called on an instance that has not
        been started, so use only ``config``. Raise or return nothing and the form keeps its plain box."""
        return {}

    def trace(self, kind: str, /, **data: Any) -> None:
        """Record an event of your own in the trace. Does nothing unless tracing is on."""
        ctx = getattr(self, "ctx", None)
        if ctx is not None:
            ctx.trace(kind, **data)

    async def start(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        if self.config_model is not None:
            try:
                self.config = self.config_model.model_validate(ctx.config)
            except ValidationError as exc:
                fields = ", ".join(str(e["loc"][0]) if e["loc"] else str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())  # a check on the whole table has no field to name
                raise PluginError(
                    f"{ctx.name}: invalid [plugins.{ctx.kind}.{ctx.name}] settings ({fields})"
                ) from None

    async def stop(self) -> None:
        return None

    async def health(self) -> Health:
        return Health(True)

    def operations(self) -> list[PluginOperation]:
        """The methods marked with ``@command``. Override to add operations by hand, and call
        ``super().operations()`` to keep these."""
        found: list[PluginOperation] = []
        for attr in sorted(dir(type(self))):
            spec = getattr(getattr(type(self), attr, None), "_oaken_command", None)
            if spec is None:
                continue
            preview = getattr(self, spec["preview"]) if spec["preview"] else None
            found.append(
                PluginOperation(
                    name=spec["name"], summary=spec["summary"], permission=spec["permission"],
                    input=spec["input"], output=spec["output"], handler=getattr(self, attr),
                    preview=preview, mcp=spec["mcp"],
                )  # fmt: skip
            )
        return found


class BaseProvider(BasePlugin):
    """A source: it gets items from outside (a file, a socket, a service) and puts each into intake with
    ``submit``. Core knows no noun: what an item is (a line of JSON, a fax, a message) is the product's, and the
    body goes in as given. Run your own loop or server from ``start`` and end it in ``stop``."""

    kind: ClassVar[str] = "provider"

    async def submit(
        self, body: Any, *, source: str | None = None, labels: dict[str, str] | None = None, key: str | None = None,
        format: str | None = None,
    ) -> str:
        """Put one item into intake (triage, then the ingestor). Returns its provisional subject id. ``source`` is
        what the record calls where it came from (default: this plugin's name). ``key`` is your own id for the
        item (a message id, a URL): a dedupe triager drops a second item with the same key, so a poller can
        safely offer what it already sent. ``format`` (``json``, ``ini``, a format a formatter plugin declares) says what the
        body is, so a formatter splits it into parts before triage; none keeps it one ``body`` part. Raises ``PluginError`` in a bare context with no intake."""
        submit = self.ctx.submit
        if submit is None:
            raise PluginError(f"{self.ctx.name}: no intake in this context")
        return await submit(source or self.ctx.name, body, labels=labels or {}, key=key, format=format)


class BaseStore(BasePlugin):
    """A store hands core a ``Database``. Core and plugins write portable SQL against it."""
    kind: ClassVar[str] = "store"

    @_default
    async def open(self) -> Database:
        raise NotImplementedError("a store must implement open()")


class BaseTemplatePlugin(BasePlugin):
    """Add to a plugin's bases when it only runs as a named instance that is given what makes it what it is: an agent's
    instructions, an account, a mailbox. ``class Mine(BaseTemplatePlugin, BaseTriager)`` with ``contextual = ("criteria",)``.

    Bare, it is a template, not a failure: it never starts, the console says so, and the **+** on its page makes an instance
    that must set every ``contextual`` setting. Only a template gets that button. Say what it is for in the class docstring,
    ``setup_help`` and ``config_example``: the plugin's page shows them."""

    # The settings an instance must set; they are required where an instance is made.
    contextual: ClassVar[tuple[str, ...]] = ()
    # What the console says about a template, to the person who runs it (the class docstring above is for its author).
    template_help: ClassVar[str] = (
        "This is a template: it does not run as itself. Each instance you make from it (the + on a stage's page) is given what makes it "
        "what it is, starts from this template's settings, and may override any of them. Switched off, it makes no new instances."
    )

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("kind") or any(getattr(b, "kind", "") for b in cls.__mro__[1:] if b is not BaseTemplatePlugin and b is not BasePlugin):
            if not cls.contextual:  # a template with nothing to be given is not one
                raise TypeError(f"{cls.__name__} is a BaseTemplatePlugin: name what an instance must be given in `contextual`")


class BaseAnalyzer(BasePlugin):
    """What triagers, enrichers and actions share. Deterministic by default; a plugin that asks a
    model also extends ``BaseLLMAnalyzer``."""

    version: ClassVar[str] = "1"
    needs_llm: ClassVar[bool] = False


class BaseVecAnalyzer(BaseAnalyzer):
    """Add to a kind's base for an analyzer that uses the embedder and vector store, only through
    the router (``embed``, ``nearest``): ``class Mine(BaseVecAnalyzer, BaseTriager)``. Its jobs run
    in core, never on an agent, and wait while no embedder or vector store is healthy."""

    needs_vec: ClassVar[bool] = True


class BaseLLMAnalyzer(BaseAnalyzer):
    """Add to a kind's base for an analyzer that asks a model through the router:
    ``class Mine(BaseLLMAnalyzer, BaseTriager)``. Sets ``needs_llm``, so the console shows the
    plugin's ``prompt`` and ``guard`` (locked until the owner overrides them).

    Declare the default ``route`` as a dict (``{"backend": "desk", "model": "qwen3", "fallback":
    ["laptop"], "escalate": {"backend": "big", "model": "qwen3-72b"}}``); the owner overrides it
    in ``[plugins.<kind>.<name>.route]``. ``prompt`` is the instruction the model gets and
    ``guard`` the standing rules around outside text. Read them with ``effective_prompt()`` and
    ``effective_guard()``: the owner's override (``prompt`` and ``guard`` in the plugin's table) wins."""

    needs_llm: ClassVar[bool] = True
    prompt: ClassVar[str] = ""
    guard: ClassVar[str] = ""
    route: ClassVar[dict[str, Any]] = {}

    def effective_prompt(self) -> str:
        """The owner's override if there is one, else the plugin's ``prompt``."""
        return self.ctx.prompt or self.prompt

    def effective_guard(self) -> str:
        return self.ctx.guard or self.guard

    def _route(self) -> "Route":
        from oaken_core.router.router import Route

        return Route.from_config(self.ctx.route, Route.from_config(self.route))

    async def ask(
        self, request: Any, output: type[M], *, content_hash: str, confidential: bool = False, text: str = ""
    ) -> M:
        """One question through the router: the cache, then (if ``needs_vec`` and ``text`` is given)
        a near-duplicate's answer, then the model. The answer is validated against ``output``; a bad
        one fails the job and stores nothing. The cache key is (this analyzer, ``version``,
        ``content_hash``, the effective model). ``confidential`` keeps the call off remote boxes."""
        from oaken_core.router.router import Call

        router = self.ctx.router
        if router is None:
            raise PluginError(f"{self.ctx.name}: no model router in this context")
        call = Call(self.ctx.name, str(self.version), content_hash, confidential, getattr(self, "needs_vec", False), text)
        return await router.ask(self._route(), call, request, output)


class BaseEmbedder(BasePlugin):
    """Turns text into vectors. Analyzers reach it only through the router (``embed``)."""
    kind: ClassVar[str] = "embedder"

    model_id: str = ""
    dimensions: int = 0
    max_input_tokens: int = 8192

    @_default
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError("an embedder must implement embed()")


class BaseLLMBackend(BasePlugin):
    """Runs a chat model. The router calls ``generate``; where the model runs (``locality``) is the
    instance's table, not the class, so the owner decides who may send it what."""
    kind: ClassVar[str] = "llm_backend"

    model_id: str = ""
    context_window: int = 0

    def thinking_for(self, request: LLMRequest) -> bool | None:
        """Whether to let the model reason on this call, or None to leave it to the model. A ``think``
        in the plugin's own settings wins over what the request asks for."""
        chosen = getattr(self.config, "think", None) if self.config is not None else None
        return chosen if isinstance(chosen, bool) else request.think

    @_default
    async def generate(self, request: LLMRequest, *, model: str | None = None) -> LLMResponse:
        raise NotImplementedError("an LLM backend must implement generate()")

    async def available_models(self) -> list[str]:
        """The model ids it can run right now, for a picker to offer. The default is the one it started
        with; a backend that can enumerate its models overrides this."""
        return [self.model_id] if self.model_id else []


@dataclass(frozen=True)
class VectorStats:
    """What a store holds: ``count`` vectors in all (or in the one collection asked about), of
    ``dimensions`` (0 when it spans collections), and the count per collection."""

    count: int
    dimensions: int = 0
    collections: dict[str, int] = field(default_factory=dict)


class BaseVectorStore(BasePlugin):
    """Stores vectors under a collection and a key, and finds the nearest. A collection never mixes
    vector sizes, and one collection never sees another's vectors. ``score`` is cosine similarity."""
    kind: ClassVar[str] = "vector_store"

    @_default
    async def upsert(self, collection: str, items: Sequence[VectorItem]) -> None:
        raise NotImplementedError("a vector store must implement upsert()")

    @_default
    async def delete(self, collection: str, keys: Sequence[str]) -> None:
        raise NotImplementedError("a vector store must implement delete()")

    @_default
    async def query(self, collection: str, vector: Sequence[float], k: int) -> list[Hit]:
        raise NotImplementedError("a vector store must implement query()")

    @_default
    async def stats(self, collection: str | None = None) -> VectorStats:
        raise NotImplementedError("a vector store must implement stats()")


class BaseCache(BasePlugin):
    """The router's answer cache, swapped for another store. Override all five members. ``key`` may return
    any tuple of strings; ``get`` and ``put`` get that tuple back. Core's own (``AnswerCache``, a table in
    core's database) is what runs unless ``[router] cache_store`` names a healthy instance of one of these."""
    kind: ClassVar[str] = "cache_store"

    @_default
    def key(self, analyzer: str, version: str, content_hash: str, model: str) -> tuple[str, ...]:
        raise NotImplementedError

    @_default
    async def get(self, key: tuple[str, ...]) -> CacheEntry | None:
        raise NotImplementedError

    @_default
    async def put(self, key: tuple[str, ...], entry: CacheEntry) -> None:
        raise NotImplementedError

    @_default
    async def stats(self) -> CacheStats:
        raise NotImplementedError

    @_default
    async def clear(self, analyzer: str | None = None) -> int:
        raise NotImplementedError


class BaseIngestor(BasePlugin):
    """Stores an item intake accepted and returns its real ``SubjectId``. The product's own: core
    knows nothing about what an item is. One is active at a time."""
    kind: ClassVar[str] = "ingestor"

    @_default
    async def ingest(self, item: Any) -> Any:
        raise NotImplementedError("an ingestor must implement ingest()")

    @_default
    async def read(self, subject_id: str, part: str | None = None) -> dict[str, Any]:
        """The stored item as ``{part name: content}`` (JSON data), or just ``{part: content}`` when ``part`` is given. This is how
        an item's context is fetched back (``oaken item show``) and how an analyzer asks for the part it reads. An ingestor that
        cannot say leaves this alone. Raise ``NotFound`` for a subject or a part it does not have."""
        raise NotImplementedError("this ingestor cannot read items back")


class BaseFormatter(BasePlugin):
    """Splits a raw item into named parts (``oaken_core.parts.Part``) before triage. ``formats`` lists
    the ``format`` strings it handles (``email/rfc822``); an item whose format nobody handles stays one
    ``body`` part. Deterministic: no model, nothing stored."""
    kind: ClassVar[str] = "formatter"

    formats: ClassVar[tuple[str, ...]] = ()

    @_default
    async def split(self, raw: Any) -> Any:
        raise NotImplementedError("a formatter must implement split()")


class BaseTriager(BaseAnalyzer):
    """Rates an intake item before it is stored. Lowest ``rank`` first; the first verdict wins."""
    kind: ClassVar[str] = "triager"

    rank: ClassVar[int] = 50

    @_default
    async def triage(self, item: Any) -> Any:
        raise NotImplementedError("a triager must implement triage()")


class BaseEnricher(BaseAnalyzer):

    kind: ClassVar[str] = "enricher"

    @_default
    async def enrich(self, target: Any) -> Any:
        raise NotImplementedError("an enricher must implement enrich()")


class BaseAction(BaseAnalyzer):

    kind: ClassVar[str] = "action"
    routes: ClassVar[tuple[str, ...]] = ()

    @_default
    async def act(self, target: Any) -> Any:
        raise NotImplementedError("an action must implement act()")
