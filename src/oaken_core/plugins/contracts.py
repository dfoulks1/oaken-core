# SPDX-License-Identifier: Apache-2.0
"""What the host and a plugin say to each other.

Adapted from oaken-mail ``plugins/contracts.py`` (cf3b187). Kept: the context, health, operations,
the UI-as-data types, job handlers and scheduled tasks. Left out: the mail kinds' types (provider,
message, enrich output), per-account instances and the pipeline stage hook (queues replace it).
Contracts are structural: any object with the members of its kind works.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from oaken_core.clock import Clock
from oaken_core.db import Database
from oaken_core.errors import PluginError
from oaken_core.events import Event, EventBus, PluginEvent
from oaken_core.untrusted import UntrustedText

if TYPE_CHECKING:
    from oaken_core.labels import LabelStore

# 1: the first Oaken Core plugin API.
PLUGIN_API_VERSION = 1


@dataclass(frozen=True)
class PluginInfo:
    """What the host knows about a loaded plugin."""

    name: str
    version: str
    kind: str
    api_version: int


@dataclass(frozen=True)
class Health:
    ok: bool
    detail: str = ""


def _no_trace(kind: str, /, **data: Any) -> None:
    return None


def _not_tracing() -> bool:
    return False


class PluginLabels(Protocol):
    """A plugin's labels: the real store with the plugin as the actor. A bad write raises
    ``LabelError``, and the host switches the plugin off."""

    async def set(self, subject_id: str, key: str, value: str) -> bool: ...
    async def get(self, subject_id: str, key: str) -> str | None: ...
    async def layers(self, subject_id: str, key: str) -> dict[str, str]: ...
    async def all(self, subject_id: str) -> dict[str, str]: ...


@dataclass(frozen=True)
class PluginContext:
    """Handed to ``start()``. ``config`` is the plugin's own ``[plugins.<kind>.<name>]`` table.
    ``name`` is the instance name; ``plugin`` is the entry point it is an instance of."""

    name: str
    kind: str
    clock: Clock
    log: Any
    plugin: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    bus: EventBus | None = None
    # The database, for a plugin that declares ``migrations_dir`` and owns tables. None for the
    # store itself. shortcut: plugins run in-process and are not sandboxed; upgrade to a table
    # facade if untrusted plugins ever matter.
    db: Database | None = None
    labels: PluginLabels | None = None
    trace: Callable[..., None] = _no_trace
    tracing: Callable[[], bool] = _not_tracing
    folders: Mapping[str, Path] = field(default_factory=dict)
    # The model router (``oaken_core.router.Router``) and this plugin's ``route`` table: which
    # backend and model it asks, its fallbacks and its input budget. None in a bare test context.
    router: Any = None
    route: dict[str, Any] = field(default_factory=dict)
    # The owner's override of an LLM analyzer's ``prompt`` and ``guard`` ("" = use the plugin's own).
    prompt: str = ""
    guard: str = ""
    # ``await ctx.submit(source, body, labels={...})`` puts an item into intake (triage, then the
    # ingestor) and returns its provisional subject id. A provider's way in. None in a bare context.
    submit: Callable[..., Awaitable[str]] | None = None
    # This plugin's own credentials in the OS keyring: ``get``, ``set``, ``delete`` by name. Never put a
    # secret in ``config`` (the config file refuses it). None in a bare test context.
    secrets: Any = None
    _subscriptions: list[Callable[[], None]] = field(
        default_factory=list, repr=False, compare=False
    )

    async def publish(self, event: PluginEvent) -> None:
        """Announce a plugin-defined event (a ``PluginEvent`` subclass) on the event bus. Core
        events cannot be forged this way. A context without a bus (tests) drops it quietly."""
        if not isinstance(event, PluginEvent):
            raise PluginError(f"{self.name}: publish() takes a PluginEvent subclass")
        if self.bus is not None:
            await self.bus.publish(replace(event, plugin=self.name))

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], Awaitable[None]]
    ) -> Callable[[], None]:
        """Call ``handler(event)`` for every core or plugin event of ``event_type``. Read-only;
        a handler that raises is logged and skipped. Subscriptions end when the plugin stops."""
        if self.bus is None:
            return lambda: None
        unsubscribe = self.bus.subscribe(event_type, handler)
        self._subscriptions.append(unsubscribe)
        return unsubscribe

    def release(self) -> None:
        """End every subscription this plugin made. Called by the host when the plugin stops."""
        subs, self._subscriptions[:] = list(self._subscriptions), []
        for unsubscribe in subs:
            unsubscribe()


# -- Operations a plugin offers ----------------------------------------------------------------
# Any plugin, of any kind, may offer operations by defining ``operations()``. Core adds them to the
# operation registry as ``<plugin>.<name>``, so the CLI, the console and (if the user allows it)
# MCP get them with the same permission classes, confirm gate and audit as core's own.


@dataclass(frozen=True)
class PluginOpContext:
    """What an operation handler of a plugin receives besides its input. No database handle: a
    plugin reaches its labels, its ``config`` and the clock, nothing else."""

    plugin: str
    client: str  # which door the call came through: cli, ui, mcp or internal
    actor: str  # who: a label, never a secret
    config: dict[str, Any]
    clock: Clock
    log: Any
    labels: PluginLabels | None = None


@dataclass(frozen=True)
class PluginChange:
    """One line of a proposal: what would change. ``target`` is an id, never private text."""

    target: str
    field: str = ""
    before: Any = None
    after: Any = None


@dataclass(frozen=True)
class PluginPreview:
    """What a mutating operation would do, shown to the person before it does it."""

    summary: str
    changes: tuple[PluginChange, ...] = ()
    total: int = 0


PluginOpHandler = Callable[[PluginOpContext, Any], Awaitable[Any]]
PluginOpPreview = Callable[[PluginOpContext, Any], Awaitable[PluginPreview]]


@dataclass(frozen=True)
class PluginOperation:
    """One operation a plugin offers.

    ``permission`` is ``read`` or ``mutate``. A ``mutate`` is confirmed by the person at the CLI or
    console, and by proposal and token over MCP; give it a ``preview`` to say what it would change.
    ``mcp`` is ``off`` (the user may turn it on) or ``never`` (they may not): a plugin's operations
    are never on by default. ``input`` and ``output`` are pydantic models.
    """

    name: str
    summary: str
    permission: Literal["read", "mutate"]
    input: type[BaseModel]
    output: type[BaseModel]
    handler: PluginOpHandler
    preview: PluginOpPreview | None = None
    mcp: Literal["off", "never"] = "off"


# -- A plugin's UI, as data (oaken-mail ADR 0018) ----------------------------------------------
# A plugin declares its UI in ``ui()`` as these plain dataclasses and never sends markup. Core owns
# the chrome, the escaping, the length caps, the CSRF token and the CSP. A button or page names the
# plugin's own operations, and core refuses any other.


@dataclass(frozen=True)
class UiField:
    """One input of a page. ``kind`` is text, number, bool, enum (``choices``), list (one string a
    line) or textarea (one string, many lines). ``name`` is the key in the page's ``load`` values and in what ``test`` and ``submit``
    receive."""

    name: str
    label: str = ""
    kind: Literal["text", "number", "bool", "enum", "list", "textarea"] = "text"
    choices: tuple[str, ...] = ()
    help: str = ""
    required: bool = False


@dataclass(frozen=True)
class UiButton:
    """A button on a plugin's settings modal: runs one of the plugin's own operations, which takes
    no input."""

    label: str
    operation: str
    confirm: str = ""


@dataclass(frozen=True)
class UiSection:
    """A titled group of the plugin's config fields, in the settings modal."""

    title: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class UiPage:
    """A page core serves at ``/plugins/<plugin>/<name>`` and renders as a form.

    ``load`` is a read operation of the plugin with no input; it returns
    ``{"values": {field: value}, "messages": [str], "rows": [{"id", "label"}]}`` (each part
    optional). ``test`` (read) and ``submit`` (the plugin's own, a mutate is confirmed) take the
    typed fields as input. With ``order`` set, the page lists ``rows`` in order, each with a
    position
    box (1 to n), and Update calls the operation ``order`` with ``{"order": [row ids]}``: a row
    whose number changed goes there and the others shift to make room ."""

    name: str
    title: str
    load: str
    submit: str = ""
    fields: tuple[UiField, ...] = ()
    test: str = ""
    confirm: str = ""
    order: str = ""


@dataclass(frozen=True)
class UiPill:
    """One status pill in a plugin's row. Core draws it; the plugin only says what it reads and how serious it is."""

    text: str
    tone: Literal["ok", "warn", "bad", "muted"] = "muted"


# What a row's button can be. Core draws each the same way on every page. Making an instance is not a row's action: a table that holds a
# template gets one button in its header.
UI_ACTIONS = ("settings", "reload", "remove")


@dataclass(frozen=True)
class UiAction:
    kind: Literal["settings", "reload", "remove"]


@dataclass(frozen=True)
class UiRow:
    """A plugin's row in any table of plugins: pills beside core's own health pills, and the buttons of its Actions cell. The plugin
    declares data; core draws the same row on every page."""

    status: tuple[UiPill, ...] = ()
    actions: tuple[UiAction, ...] = ()


@dataclass(frozen=True)
class UiTemplate:
    """The part of a spec that makes a plugin a template, owned by ``BaseTemplatePlugin``: the settings an instance must be given
    (``contextual``; an entry ``a|b`` means one of them) and what to tell the person making one. Core draws the template's pill and its
    **+ New instance** from this, the same on every page, so no page decides it for itself."""

    contextual: tuple[str, ...]
    help: str = ""


@dataclass(frozen=True)
class UiSpec:
    sections: tuple[UiSection, ...] = ()
    buttons: tuple[UiButton, ...] = ()
    pages: tuple[UiPage, ...] = ()
    template: UiTemplate | None = None
    row: UiRow | None = None
    fields: tuple[UiField, ...] = ()  # overrides: each replaces what the config schema made for the setting of that name (label, kind, help, choices, required)


# -- Job kinds a plugin offers -----------------------------------------------------------------
# A plugin may run its own background work by defining ``job_handlers()``. Core registers each on
# the job runner as ``plugin.<name>.<kind>`` (namespaced so two plugins cannot collide), with the
# same durable queue, retries, leases and per-kind concurrency as core's own kinds. The handler is
# given a plugin-scoped context (no database handle) and its validated payload; it never touches
# the queue, the stores or another plugin.


# -- Job kinds a plugin offers -----------------------------------------------------------------
# A plugin may run its own background work by defining ``job_handlers()``. Core registers each on
# the job runner as ``plugin.<name>.<kind>`` (namespaced so two plugins cannot collide), with the
# same durable queue, retries, leases and per-kind concurrency as core's own kinds. The handler is
# given a plugin-scoped context (no database handle) and its validated payload.


@dataclass(frozen=True)
class PluginJobContext:
    """What a plugin's job handler receives besides its payload. Like ``PluginOpContext``: the
    plugin's own ``config``, the clock and a logger, and the registered ``kind`` it is running —
    no database handle, no queue, no job internals."""

    plugin: str
    kind: str  # the namespaced kind, e.g. "plugin.word-count.recount"
    config: dict[str, Any]
    clock: Clock
    log: Any


# ``handler(ctx, payload) -> result | None``. ``payload`` is an instance of the handler's
# ``payload_model``; a returned value must match ``result_model`` when one is declared.
PluginJobHandlerFn = Callable[["PluginJobContext", Any], Awaitable[Any]]


@dataclass(frozen=True)
class PluginJobHandler:
    """One job kind a plugin offers.

    ``kind`` is the plugin's local name (``recount``); core namespaces it to
    ``plugin.<name>.<kind>``. ``payload_model`` and ``result_model`` are pydantic models: the
    payload is validated before the handler runs, and a result (if ``result_model`` is set) is
    validated and stored, exactly as for a core job kind. ``concurrency`` caps how many run at
    once (``[jobs] kind_limits`` still wins). A plugin enqueues its kind with ``ctx`` helpers added
    later; for now core and other plugins can enqueue it by its namespaced name.
    """

    kind: str
    payload_model: type[BaseModel]
    handler: PluginJobHandlerFn
    concurrency: int | None = None
    result_model: type[BaseModel] | None = None


# -- Pipeline stages a plugin offers -----------------------------------------------------------
# A plugin may add a derivation stage that runs **after the enrichers** for a message, by defining
# ``pipeline_stages()``. It sees the message and the enrichment results just computed, and returns
# JSON-serializable data core stores with the same content-hash idempotency as an enricher — so a
# rerun or a reprocess is a cache hit, never recomputed. A stage never touches the databases, runs
# a model directly, or sees another plugin's state; it is for cheap, deterministic post-processing
# (a tag derived from triage, a cross-enricher roll-up).



# -- Scheduled tasks a plugin offers -----------------------------------------------------------
# A plugin may schedule recurring work by defining ``scheduled_tasks()``. Core seeds each into the
# scheduler as ``plugin.<name>.<task>`` **only if it is absent**, so a schedule the user has since
# edited (interval, cron, or removed) is left alone. The task fires a ``job_kind`` with a fixed
# ``payload``.


@dataclass(frozen=True)
class PluginScheduledTask:
    """One recurring task a plugin offers. Give exactly one of ``every`` (seconds) or ``cron`` (a
    five-field expression). ``job_kind`` is the kind enqueued when it fires — commonly the plugin's
    own ``plugin.<name>.<kind>`` — and ``payload`` is the fixed input for that job.
    """

    name: str
    job_kind: str
    payload: BaseModel
    every: float | None = None
    cron: str | None = None


# -- Models and vectors -------------------------------------------------------------------------
# A backend runs a model; an embedder turns text into vectors; a vector store finds the nearest.
# Analyzers never call these directly: they go through the router (``BaseLLMAnalyzer.ask``).


@dataclass(frozen=True)
class LLMRequest:
    """``system`` is a static instruction. Outside text only ever appears in ``user``, as
    ``UntrustedText`` (plain ``str`` parts are for the application's own words)."""

    system: str
    user: tuple[str | UntrustedText, ...] = ()
    json_schema: dict[str, Any] | None = None
    max_output_tokens: int | None = None
    temperature: float = 0.0
    think: bool | None = None  # False: answer without reasoning; None: the backend decides


@dataclass(frozen=True)
class LLMResponse:
    text: str = ""
    data: dict[str, Any] | None = None  # parsed JSON when a schema was requested
    input_tokens: int = 0
    output_tokens: int = 0
    model_id: str = ""
    thinking: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CacheEntry:
    """A validated model answer and what it cost to get, as the answer cache stores it."""

    result_json: str
    tokens_in: int
    tokens_out: int


@dataclass(frozen=True)
class CacheStats:
    entries: int
    bytes: int = 0
    by_analyzer: dict[str, int] = field(default_factory=dict)
    oldest: datetime | None = None


@dataclass(frozen=True)
class VectorItem:
    """One vector under a ``key`` (a content hash). A store never mixes collections."""

    key: str
    vector: tuple[float, ...]


@dataclass(frozen=True)
class Hit:
    key: str
    score: float  # cosine similarity: 1.0 is the same direction


@runtime_checkable
class Plugin(Protocol):
    """Lifecycle, shared by every kind. All three are optional for a plugin to define."""

    async def start(self, ctx: PluginContext) -> None:
        """Acquire resources. Raising marks the plugin unhealthy; core carries on."""
        ...

    async def stop(self) -> None:
        """Release resources. Called once at shutdown."""
        ...

    async def health(self) -> Health:
        """A cheap liveness check, polled periodically by the host."""
        ...
