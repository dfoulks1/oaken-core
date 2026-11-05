# SPDX-License-Identifier: Apache-2.0
"""The plugin host: discover, validate, start, watch and stop plugins.

Adapted from oaken-mail ``plugins/host.py`` (cf3b187). A plugin that fails anywhere (import,
validation, ``start()``, a health check) is recorded as unhealthy, logged, announced with
``PluginHealthChanged`` and left out of lookups. Core goes on running. A plugin whose ``start()``
failed is tried again at each health tick.

What differs from oaken-mail: kinds are a registry (``kinds.py``), not a closed list. The store is
a plugin and starts first (``start_store``), then core's migrations run, then the rest start.
Several named instances of one plugin come from config tables that say ``plugin = "<name>"``. A
plugin may own tables (its ``migrations_dir``) and define label keys. A label error switches the
plugin off. ``requires_core`` is checked, and ``needs`` must be acyclic.

Plugins run in-process with full access and are not sandboxed.
"""

import asyncio
import contextlib
import inspect
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from importlib.metadata import EntryPoint, entry_points
from typing import Any, cast

from pydantic import ValidationError

from oaken_core import __version__
from oaken_core.actors import Actor
from oaken_core.clock import Clock
from oaken_core.db import Database, migrate
from oaken_core.errors import NotFound, PluginError, Unavailable
from oaken_core.events import EventBus, PluginHealthChanged
from oaken_core.labels import LabelError, LabelMaker, LabelStore
from oaken_core.logging import get_logger
from oaken_core.plugins.contracts import (
    Health,
    PluginContext,
    PluginInfo,
    PluginJobContext,
    PluginJobHandler,
    PluginOpContext,
    PluginOperation,
    PluginScheduledTask,
    UiSpec,
)
from oaken_core.secrets import PluginSecrets
from oaken_core.plugins.kinds import KindRegistry, KindSpec
from oaken_core.plugins.ui import check_ui
from oaken_core.plugins.validate import (
    NEED,
    check_plugin,
    check_started,
    declared_job_handlers,
    declared_operations,
    declared_scheduled_tasks,
)
from oaken_core.settings import PluginTables
from oaken_core.store import MIGRATIONS
from oaken_core.trace import Tracer

log = get_logger("oaken_core.plugins")

_INSTANCE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")
HOST_KEYS = ("enabled", "plugin", "locality", "concurrency", "route", "prompt", "guard")  # the host's settings, never the plugin's
RELATIONS = frozenset({"self", "parent", "children"})  # a product adds more with register_relation

Discover = Callable[[str], Iterable[EntryPoint]]


def discover_entry_points(group: str) -> Iterable[EntryPoint]:
    return entry_points(group=group)


@dataclass
class _Record:
    kind: str
    name: str  # the instance name: the entry point's, or a config table's
    plugin: str  # the entry point this is an instance of
    info: PluginInfo | None = None
    instance: Any = None
    started: bool = False
    healthy: bool = False
    detail: str = ""
    announced: bool | None = None  # the last health state published
    ctx: PluginContext | None = None
    factory: Any = None  # what the entry point loaded: a class or function that makes instances
    disabled: bool = False  # turned off at runtime, or broken; ``enabled = false`` also counts
    forced_on: bool = False  # ``enable`` was asked for: beats the plugin's own off-by-default
    auto_disabled: bool = False  # switched off for want of configuration: retried, not forced off
    faulted: bool = False  # switched off for a fault (label error, needs cycle): ``enable`` retries
    inactive: bool = False  # an exclusive kind with another plugin active


@dataclass(frozen=True)
class OfferedOperation:
    plugin: str
    declaration: PluginOperation


@dataclass(frozen=True)
class OfferedJobHandler:
    plugin: str
    declaration: PluginJobHandler


@dataclass(frozen=True)
class OfferedScheduledTask:
    plugin: str
    declaration: PluginScheduledTask


@dataclass(frozen=True)
class PluginStatus:
    kind: str
    name: str
    plugin: str
    version: str
    started: bool
    healthy: bool
    detail: str
    enabled: bool = True
    enabled_default: bool = True


@dataclass(frozen=True)
class Probe:
    """What starting a throwaway instance of a plugin on a candidate config showed."""

    kind: str
    ok: bool
    detail: str = ""


class _PluginLabels:
    """``ctx.labels``: the real label store with the plugin as the actor. A bad write switches the
    plugin off before the error reaches it."""

    def __init__(self, host: "PluginHost", rec: _Record, store: LabelStore) -> None:
        self._host, self._rec, self._store = host, rec, store
        self._actor = Actor("system", f"{rec.kind}.{rec.name}")

    async def set(self, subject_id: str, key: str, value: str) -> bool:
        try:
            return await self._store.set(subject_id, key, value, self._actor)
        except LabelError as exc:
            await self._host._fault(self._rec, exc)
            raise

    async def get(self, subject_id: str, key: str) -> str | None:
        return await self._store.get(subject_id, key)

    async def layers(self, subject_id: str, key: str) -> dict[str, str]:
        return await self._store.layers(subject_id, key)

    async def all(self, subject_id: str) -> dict[str, str]:
        return await self._store.all(subject_id)


class PluginHost:
    def __init__(
        self,
        tables: PluginTables,
        clock: Clock,
        bus: EventBus | None = None,
        *,
        kinds: KindRegistry | None = None,
        discover: Discover = discover_entry_points,
        health_interval: float = 30.0,
        call_timeout: float = 30.0,
        core_version: str = __version__,
    ) -> None:
        self.tables = tables
        self.clock = clock
        self.bus = bus
        self.kinds = kinds or KindRegistry.shipped()
        self.tracer: Tracer | None = None  # set by core: the opt-in trace
        self.router: Any = None  # set by core: the model router, handed to plugins as ``ctx.router``
        self.intake: Any = None  # set by core: ``Intake``, behind ``ctx.submit``
        self.secrets: Any = None  # set by core: a ``SecretStore``, handed to each plugin as its own ``ctx.secrets``
        self._discover = discover
        self.health_interval = health_interval
        self.call_timeout = call_timeout
        self.core_version = core_version
        self.db: Database | None = None  # opened by the store plugin, in ``start_store``
        self.maker: LabelMaker | None = None  # attach with ``attach_labels`` before ``start``
        self.label_store: LabelStore | None = None
        self.relations: set[str] = set(RELATIONS)
        self._records: dict[tuple[str, str], _Record] = {}
        self._migrated: set[tuple[str, str]] = set()
        self._loaded = False
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def attach_labels(self, store: LabelStore, maker: LabelMaker) -> None:
        """Give plugins their labels: ``ctx.labels`` writes through ``store``, and the keys a
        plugin's ``labels()`` returns are registered on ``maker``."""
        self.label_store, self.maker = store, maker

    def register_relation(self, name: str) -> None:
        """Let a product add a ``needs`` relation (``self``, ``parent`` and ``children`` ship)."""
        self.relations.add(name)

    # -- lifecycle ---------------------------------------------------------------------------
    async def load(self) -> None:
        """Find every installed plugin and validate it, then the named instances the config asks
        for. Nothing is started yet."""
        for spec in self.kinds.ordered():
            for ep in sorted(self._discover(spec.group), key=lambda e: e.name):
                if (spec.name, ep.name) in self._records:
                    log.error("duplicate plugin name ignored", kind=spec.name, name=ep.name)
                    continue
                self._records[(spec.name, ep.name)] = self._load_one(spec, ep)
        for spec in self.kinds:
            for name, base in self.tables.instances(spec.name).items():
                if (spec.name, name) not in self._records:
                    self._records[(spec.name, name)] = self._instance_of(spec, name, base)
        self._check_needs()
        self._loaded = True

    def _load_one(self, spec: KindSpec, ep: EntryPoint) -> _Record:
        rec = _Record(spec.name, ep.name, ep.name)
        try:
            obj = ep.load()
            rec.factory = obj
            rec.instance = self._instantiate(obj)
            info, problems = check_plugin(spec, ep.name, rec.instance, self.core_version, defer_started=True)
            if info is not None and not info.version and ep.dist is not None:
                info = replace(info, version=ep.dist.version)
            if problems:
                raise PluginError("; ".join(problems))
            rec.info = info
        except Exception as exc:  # a broken plugin must never take core down
            rec.instance, rec.healthy, rec.disabled = None, False, True
            rec.detail = f"failed to load: {exc}"
            log.error("plugin failed to load", kind=spec.name, name=ep.name, error=str(exc))
        return rec

    def _instance_of(self, spec: KindSpec, name: str, base: str) -> _Record:
        """A named instance: another object made from plugin ``base``'s factory."""
        rec = _Record(spec.name, name, base)
        found = self._records.get((spec.name, base))
        if found is None or found.factory is None:
            rec.disabled, rec.detail = True, f"failed to load: no {spec.name} plugin named {base!r}"
        elif not (isinstance(found.factory, type) or inspect.isfunction(found.factory)):
            rec.disabled, rec.detail = True, f"{base} is a single object: it has no named instances"
        else:
            try:
                rec.factory, rec.instance = found.factory, self._instantiate(found.factory)
                rec.info = replace(found.info, name=name) if found.info else None
                rec.detail = "" if found.instance is not None else found.detail
                if found.instance is None:
                    rec.instance, rec.disabled = None, True
            except Exception as exc:
                rec.disabled, rec.detail = True, f"failed to load: {exc}"
        return rec

    def check_new_instance(self, kind: str, name: str, plugin: str, config: dict[str, Any]) -> list[str]:
        """Why ``add_instance`` would refuse these, in plain words; empty when it would not. Nothing
        is changed or started."""
        spec = self.kinds.get(kind)
        if not _INSTANCE_NAME.match(name):
            return [f"{name!r} is not a valid instance name (lowercase letters, digits, - and _)"]
        if (kind, name) in self._records:
            return [f"{kind} {name!r} already exists"]
        base = self._records.get((spec.name, plugin))
        if base is None or base.instance is None:
            return [f"no usable {kind} plugin named {plugin!r}" + (f" ({base.detail})" if base else "")]
        try:
            self._validate_config(base.instance, {k: v for k, v in config.items() if k not in HOST_KEYS})
        except PluginError as exc:
            return [str(exc)]
        return []

    async def add_instance(self, kind: str, name: str, plugin: str, config: dict[str, Any]) -> None:
        """Start another named instance of ``plugin`` now, on ``config`` (a console form's "add a
        backend"). The settings are kept for the host's life; write them to the config file to
        keep them longer."""
        spec = self.kinds.get(kind)
        if (kind, name) in self._records:
            raise PluginError(f"{kind} {name!r} already exists")
        self.tables.tables.setdefault(kind, {})[name] = {**config, "plugin": plugin}
        rec = self._records[(kind, name)] = self._instance_of(spec, name, plugin)
        self._check_needs()
        if rec.instance is not None and self._loaded:
            await self._start_one(rec)
        else:
            await self._announce(rec)

    @staticmethod
    def _instantiate(obj: Any) -> Any:
        if isinstance(obj, type) or inspect.isfunction(obj):
            return obj()
        return obj

    def _check_needs(self) -> None:
        """``needs`` must be acyclic: a cycle would re-run its analyzers forever. A plugin whose
        ``needs`` close a cycle is switched off. A need on itself through ``parent`` or
        ``children`` is fine (a record's parent is a different record); on ``self`` it is a cycle.
        shortcut: a cycle through mixed relations is refused too, though some are finite; relax
        it when a product needs one."""
        graph: dict[str, set[str]] = {}

        def reaches(src: str, dst: str, seen: set[str] | None = None) -> bool:
            seen = seen if seen is not None else set()
            if src == dst:
                return True
            seen.add(src)
            return any(n not in seen and reaches(n, dst, seen) for n in graph.get(src, ()))

        for rec in sorted(self._records.values(), key=lambda r: (r.kind, r.name)):
            if rec.instance is None or rec.disabled or not self.kinds.get(rec.kind).analyzer:
                continue
            edges: set[str] = set()
            for need in getattr(rec.instance, "needs", ()):
                m = NEED.match(need)
                if m is None:
                    continue
                target, relation = m.groups()
                if relation not in self.relations:
                    self._fault_now(rec, f"needs unknown relation {relation!r}")
                    break
                if target == rec.name and relation != "self":
                    continue
                if reaches(target, rec.name):
                    self._fault_now(rec, f"needs {need!r}, which closes a cycle")
                    break
                edges.add(target)
            else:
                graph.setdefault(rec.name, set()).update(edges)

    def _fault_now(self, rec: _Record, detail: str) -> None:
        rec.disabled = rec.faulted = True
        rec.started = rec.healthy = False
        rec.detail = f"switched off: {detail}"
        log.error("plugin switched off", kind=rec.kind, name=rec.name, detail=detail)

    async def _fault(self, rec: _Record, exc: BaseException) -> None:
        """Switch a plugin off for a fault (a bad label write). ``enable`` tries it again."""
        await self._stop_one(rec)
        self._fault_now(rec, str(exc))
        await self._announce(rec)

    def _active_name(self, kind: str) -> str | None:
        """The plugin selected for ``kind``: ``tables.active``, else (for a kind with one active)
        the only usable one, else ``sqlite`` for a store."""
        name = self.tables.active.get(kind)
        if name:
            return name
        names = sorted(n for k, n in self._records if k == kind)
        if len(names) == 1:
            return names[0]
        return "sqlite" if kind == "store" and ("store", "sqlite") in self._records else None

    async def start_store(self) -> Database:
        """Start the active store plugin, open its database and apply core's migrations. The store
        starts before everything else because everything else needs it."""
        if not self._loaded:
            await self.load()
        name = self._active_name("store")
        rec = self._records.get(("store", name)) if name else None
        if rec is None or rec.instance is None:
            raise PluginError("no store plugin is available" + (f" ({rec.detail})" if rec else ""))
        await self._start_one(rec)
        if not (rec.started and rec.healthy):
            raise PluginError(f"the store {rec.name!r} did not start: {rec.detail}")
        db = await asyncio.wait_for(rec.instance.open(), self.call_timeout)
        try:
            await migrate(db, MIGRATIONS)
        except BaseException:
            await db.close()
            raise
        self.db = db
        return db

    async def start(self) -> None:
        """Start the store (if not yet), then every other valid plugin, and begin health checks."""
        if self.db is None:
            await self.start_store()
        for rec in sorted(self._records.values(), key=lambda r: (self.kinds.get(r.kind).first is False, r.kind, r.name)):
            if self.kinds.get(rec.kind).first and rec.started:
                continue
            if rec.instance is not None:
                await self._start_one(rec)
            else:
                await self._announce(rec)
        self._stop.clear()
        self._task = asyncio.create_task(self._health_loop())

    async def _switch_off(self, rec: _Record, detail: str) -> None:
        """Turn a plugin off because it lacks configuration it needs. It is tried again on the next
        health pass and on ``reload``, so it comes back by itself once the settings exist."""
        if rec.detail != detail:
            log.info("plugin switched off: it needs configuration", kind=rec.kind, name=rec.name)
        rec.disabled = rec.auto_disabled = True
        rec.started = rec.healthy = False
        rec.detail = detail
        await self._announce(rec)

    def _trace_hooks(self) -> dict[str, Any]:
        tracer = self.tracer
        return {} if tracer is None else {"trace": tracer.emit, "tracing": lambda: tracer.enabled}

    def _submitter(self, rec: _Record) -> Any:
        async def submit(source: str, body: Any, *, labels: dict[str, str] | None = None) -> str:
            if self.intake is None:
                raise Unavailable("intake is not running")
            return str(await self.intake.submit(source, body, labels=labels, actor=Actor("system", rec.name)))

        return submit

    def _config(self, rec: _Record) -> dict[str, Any]:
        return {k: v for k, v in self.tables.table(rec.kind, rec.name).items() if k not in HOST_KEYS}

    def _enabled_by_table(self, rec: _Record) -> bool:
        """The table's ``enabled`` wins. With none, a plugin runs unless it ships switched off
        (``enabled_default``) and nobody has asked for it with ``enable``."""
        explicit = self.tables.table(rec.kind, rec.name).get("enabled")
        if explicit is not None:
            return explicit is not False
        return rec.forced_on or self._enabled_default(rec.instance)

    @staticmethod
    def _enabled_default(instance: Any) -> bool:
        return getattr(instance, "enabled_default", True) is not False

    async def _start_one(self, rec: _Record) -> None:
        spec = self.kinds.get(rec.kind)
        if spec.exclusive and self._active_name(rec.kind) not in (None, rec.name):
            rec.inactive, rec.started, rec.healthy = True, False, False
            rec.detail = f"not the active {rec.kind}"
            return
        config = self._config(rec)
        if rec.auto_disabled:  # it was off for want of configuration: some may have arrived
            rec.disabled = rec.auto_disabled = False
        needs = [k for k in getattr(rec.instance, "requires_config", ()) if not config.get(k)]
        if needs:
            await self._switch_off(
                rec, f"needs configuration: set {', '.join(needs)} in [plugins.{rec.kind}.{rec.name}]"
            )
            return
        if not self._enabled_by_table(rec) or rec.disabled:
            if rec.detail != "disabled" and not rec.faulted and "failed to load" not in rec.detail:
                log.info("plugin disabled", kind=rec.kind, name=rec.name)
                rec.detail = "disabled"
            rec.started = rec.healthy = False
            await self._announce(rec)
            return
        try:
            await self._provision(rec)
        except LabelError as exc:
            await self._fault(rec, exc)
            return
        except Exception as exc:  # a bad migration or key table is the plugin's, not core's
            rec.started, rec.healthy, rec.detail = False, False, f"tables or labels failed: {exc}"
            log.error("plugin provisioning failed", kind=rec.kind, name=rec.name, error=str(exc))
            await self._announce(rec)
            return
        labels = _PluginLabels(self, rec, self.label_store) if self.label_store else None
        ctx = PluginContext(
            name=rec.name, kind=rec.kind, plugin=rec.plugin, clock=self.clock,
            log=get_logger(f"oaken_core.plugin.{rec.kind}.{rec.name}"), config=config, bus=self.bus,
            db=None if spec.first else self.db, labels=labels, router=self.router, submit=self._submitter(rec),
            secrets=PluginSecrets(self.secrets, rec.kind, rec.name) if self.secrets is not None else None,
            route=dict(self.tables.table(rec.kind, rec.name).get("route") or {}), **self._trace_hooks(),
            prompt=str(self.tables.table(rec.kind, rec.name).get("prompt") or ""),
            guard=str(self.tables.table(rec.kind, rec.name).get("guard") or ""),
        )  # fmt: skip
        rec.ctx = ctx
        try:
            self._validate_config(rec.instance, ctx.config)
            await self._call(rec.instance, "start", ctx)
            problems = check_started(spec, rec.instance)
            if problems:
                await self._call(rec.instance, "stop")
                raise PluginError("; ".join(problems))
            rec.started, rec.healthy, rec.detail = True, True, ""
        except Exception as exc:
            rec.started, rec.healthy = False, False
            if not ctx.config and not self.tables.table(rec.kind, rec.name):
                # Installed but never set up (no table): not an error, and not left half-alive:
                # it is switched off until it is configured.
                rec.disabled = rec.auto_disabled = True
                rec.detail = f"not configured ({exc})"
                log.info("plugin not configured", kind=rec.kind, name=rec.name, detail=str(exc))
            else:
                rec.detail = f"start() failed: {exc}"
                log.error("plugin failed to start", kind=rec.kind, name=rec.name, error=str(exc))
        await self._announce(rec)

    async def _provision(self, rec: _Record) -> None:
        """The plugin's tables (its ``migrations_dir``, once per plugin, however many instances)
        and label keys, before it starts."""
        directory = getattr(rec.instance, "migrations_dir", None)
        key = (rec.kind, rec.plugin)
        if directory is not None and self.db is not None and key not in self._migrated:
            table = f"schema_version_{rec.kind}_{rec.plugin}".replace("-", "_")
            await migrate(self.db, directory, table=table)
            self._migrated.add(key)
        labels = getattr(rec.instance, "labels", None)
        if callable(labels) and self.maker is not None:
            for label in labels():
                self.maker.register(label)

    async def probe(self, name: str, config: dict[str, Any], kind: str | None = None) -> list[Probe]:
        """Start a throwaway instance of plugin ``name`` on a candidate ``config`` and ask its
        health, so a form can say "this works" before anything is saved. The running plugin is not
        touched, nothing is announced, and the instance is stopped again."""
        recs = [r for (k, n), r in sorted(self._records.items()) if n == name and (kind is None or k == kind)]
        if not recs:
            raise NotFound(f"no plugin named {name!r}")
        return [await self._probe_one(r, config) for r in recs]

    async def _probe_one(self, rec: _Record, candidate: dict[str, Any]) -> Probe:
        if rec.instance is None or rec.factory is None:
            return Probe(rec.kind, False, rec.detail or "it did not load")
        if not (isinstance(rec.factory, type) or inspect.isfunction(rec.factory)):
            return Probe(rec.kind, False, "a single object: it cannot be tried apart from the live one")
        config = {k: v for k, v in candidate.items() if k not in HOST_KEYS}
        instance = self._instantiate(rec.factory)
        ctx = PluginContext(
            name=rec.name, kind=rec.kind, plugin=rec.plugin, clock=self.clock,
            log=get_logger(f"oaken_core.plugin.{rec.kind}.{rec.name}"), config=config,
            **self._trace_hooks(),
        )  # fmt: skip
        started = False
        try:
            self._validate_config(instance, config)
            await self._call(instance, "start", ctx)
            started = True
            problems = check_started(self.kinds.get(rec.kind), instance)
            if problems:
                raise PluginError("; ".join(problems))
            result = await self._call(instance, "health")
            health = result if isinstance(result, Health) else Health(True)
            return Probe(rec.kind, health.ok, health.detail)
        except Exception as exc:  # noqa: BLE001 - whatever it was is the answer
            return Probe(rec.kind, False, str(exc))
        finally:
            if started:
                try:
                    await self._call(instance, "stop")
                except Exception as exc:  # noqa: BLE001
                    log.error("probe instance failed to stop", name=rec.name, error=str(exc))
            ctx.release()

    def check_config(self, name: str, config: dict[str, Any]) -> list[str]:
        """Problems with ``config`` as the plugin's table, found without starting anything: its
        ``config_model``. Empty when it is sound."""
        recs = [r for (_k, n), r in sorted(self._records.items()) if n == name]
        if not recs:
            raise NotFound(f"no plugin named {name!r}")
        problems: list[str] = []
        table = {k: v for k, v in config.items() if k not in HOST_KEYS}
        for rec in recs:
            try:
                if rec.instance is not None:
                    self._validate_config(rec.instance, table)
            except PluginError as exc:
                problems.append(f"{rec.kind} {name}: {exc}")
        return problems

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task:
            await task
        for rec in sorted(self._records.values(), key=lambda r: self.kinds.get(r.kind).first):
            await self._stop_one(rec)  # the store last: the others may still write on the way out
        if self.db is not None:
            await self.db.close()
            self.db = None

    async def _stop_one(self, rec: _Record) -> None:
        if rec.started:
            try:
                await self._call(rec.instance, "stop")
            except Exception as exc:
                log.error("plugin failed to stop", kind=rec.kind, name=rec.name, error=str(exc))
            rec.started = False
        if rec.ctx is not None:
            rec.ctx.release()
            rec.ctx = None

    # -- control of one plugin, while core keeps running -------------------------------------
    def _family(self, name: str) -> list[_Record]:
        """The plugin called ``name`` (any kind) and every named instance of it."""
        found = [r for r in self._records.values() if r.name == name or r.plugin == name]
        if not found:
            known = ", ".join(sorted({r.name for r in self._records.values()})) or "none installed"
            raise NotFound(f"no plugin named {name!r} (found: {known})")
        return found

    async def disable(self, name: str) -> list[str]:
        """Stop a plugin and keep it stopped until ``enable``. Returns the names affected."""
        recs = self._family(name)
        for rec in recs:
            rec.disabled, rec.auto_disabled = True, False
            await self._stop_one(rec)
            rec.healthy, rec.detail = False, "disabled"
            await self._announce(rec)
        return [r.name for r in recs]

    async def enable(self, name: str) -> list[str]:
        """Start a plugin that was disabled or faulted. It still stays off if its table says
        ``enabled = false``: that is a setting, and ``reload`` picks up a change to it."""
        recs = self._family(name)
        for rec in recs:
            rec.disabled = rec.auto_disabled = rec.faulted = False
            rec.forced_on = True
            self._check_needs()
            if rec.instance is None:
                await self.reload(rec.name)
            elif not rec.started and not rec.disabled:
                await self._start_one(rec)
        return [r.name for r in recs]

    async def reload(self, name: str, tables: PluginTables | None = None) -> list[str]:
        """Stop a plugin, take a fresh instance of it (re-discovering its entry point, so a plugin
        installed or upgraded since boot is found) and start it on ``tables``. Only that plugin is
        touched; core keeps running."""
        recs = self._family(name)
        if tables is not None:
            self.tables = tables
        found = {(s.name, ep.name): ep for s in self.kinds for ep in self._discover(s.group)}
        for rec in recs:
            await self._stop_one(rec)
            ep = found.get((rec.kind, rec.plugin))
            if ep is None:
                rec.instance, rec.healthy, rec.detail = None, False, "no longer installed"
                await self._announce(rec)
                continue
            spec = self.kinds.get(rec.kind)
            fresh = self._load_one(spec, ep) if rec.name == rec.plugin else self._instance_of(spec, rec.name, rec.plugin)
            fresh.forced_on = rec.forced_on
            self._records[(rec.kind, rec.name)] = fresh
            self._check_needs()
            if fresh.instance is not None and not fresh.disabled:
                await self._start_one(fresh)
            else:
                await self._announce(fresh)
        return [r.name for r in recs]

    # -- health ------------------------------------------------------------------------------
    async def check_health(self) -> None:
        """One health pass: poll started plugins, retry the ones whose start failed."""
        for rec in list(self._records.values()):
            if rec.instance is None or rec.faulted or rec.inactive:
                continue
            if not rec.started:
                await self._start_one(rec)
                continue
            try:
                result = await self._call(rec.instance, "health")
                health = result if isinstance(result, Health) else Health(True)
                rec.healthy, rec.detail = health.ok, health.detail
            except Exception as exc:
                rec.healthy, rec.detail = False, f"health check failed: {exc}"
                log.error("plugin health check failed", kind=rec.kind, name=rec.name, error=str(exc))
            await self._announce(rec)

    async def _health_loop(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), self.health_interval)
            if not self._stop.is_set():
                await self.check_health()

    async def _call(self, obj: Any, method: str, *args: Any) -> Any:
        """Call an optional lifecycle method (a plugin need not define it), with a timeout."""
        fn = getattr(obj, method, None)
        if fn is None:
            return None
        result = fn(*args)
        if inspect.isawaitable(result):
            return await asyncio.wait_for(result, self.call_timeout)
        return result

    async def _announce(self, rec: _Record) -> None:
        if rec.announced == rec.healthy:
            return
        rec.announced = rec.healthy
        if self.bus:
            await self.bus.publish(
                PluginHealthChanged(
                    ts=self.clock.now(), plugin=f"{rec.kind}:{rec.name}",
                    healthy=rec.healthy, detail=rec.detail,
                )  # fmt: skip
            )

    # -- what plugins offer ------------------------------------------------------------------
    def _shared(self) -> list[_Record]:
        return [
            r for r in sorted(self._records.values(), key=lambda r: (r.kind, r.name))
            if r.started and r.healthy and r.instance is not None
        ]  # fmt: skip

    def _opctx(self, rec: _Record, client: str, actor: str) -> PluginOpContext:
        assert rec.ctx is not None
        return PluginOpContext(
            plugin=rec.name, client=client, actor=actor, config=dict(rec.ctx.config),
            clock=self.clock, log=rec.ctx.log, labels=rec.ctx.labels,
        )  # fmt: skip

    def offered_operations(self) -> list[OfferedOperation]:
        """What the healthy plugins offer. The registry namespaces each as ``<plugin>.<name>`` and
        the CLI builds its verbs from them, so a command appears when its plugin loads and goes
        when it does not. An operation that cannot be read is skipped (it was reported at load)."""
        out: list[OfferedOperation] = []
        for rec in self._shared():
            try:
                decls = declared_operations(rec.instance)
            except Exception as exc:
                log.error("plugin operations failed", name=rec.name, error=type(exc).__name__)
                continue
            out.extend(OfferedOperation(rec.name, d) for d in decls if isinstance(d, PluginOperation))
        return out

    def operation_context(self, plugin: str, client: str, actor: str) -> PluginOpContext:
        for rec in self._shared():
            if rec.name == plugin and rec.ctx is not None:
                return self._opctx(rec, client, actor)
        raise NotFound(f"plugin {plugin!r} is not running")

    def offered_job_handlers(self) -> list[OfferedJobHandler]:
        out: list[OfferedJobHandler] = []
        for rec in self._shared():
            try:
                decls = declared_job_handlers(rec.instance)
            except Exception as exc:
                log.error("plugin job_handlers failed", name=rec.name, error=type(exc).__name__)
                continue
            out.extend(OfferedJobHandler(rec.name, d) for d in decls if isinstance(d, PluginJobHandler))
        return out

    def job_context(self, plugin: str, kind: str) -> PluginJobContext:
        for rec in self._shared():
            if rec.name == plugin and rec.ctx is not None:
                return PluginJobContext(
                    plugin=plugin, kind=kind, config=dict(rec.ctx.config), clock=self.clock, log=rec.ctx.log
                )  # fmt: skip
        raise NotFound(f"plugin {plugin!r} is not running")

    def offered_scheduled_tasks(self) -> list[OfferedScheduledTask]:
        out: list[OfferedScheduledTask] = []
        for rec in self._shared():
            try:
                decls = declared_scheduled_tasks(rec.instance)
            except Exception as exc:
                log.error("plugin scheduled_tasks failed", name=rec.name, error=type(exc).__name__)
                continue
            out.extend(OfferedScheduledTask(rec.name, d) for d in decls if isinstance(d, PluginScheduledTask))
        return out

    def plugin_ui(self, kind: str, name: str) -> UiSpec | None:
        """The plugin's declared UI, or None. A ``ui()`` that raises or is malformed is logged and
        ignored, so the settings modal falls back to the schema and the plugin stays healthy."""
        rec = self._records.get((kind, name))
        fn = getattr(rec.instance, "ui", None) if rec and rec.instance is not None else None
        if not callable(fn):
            return None
        try:
            spec = fn()
            problems = check_ui(spec)
        except Exception as exc:  # noqa: BLE001 - plugin code
            log.error("plugin ui failed", kind=kind, name=name, error=type(exc).__name__)
            return None
        if problems:
            log.error("plugin ui refused", kind=kind, name=name, problems=problems)
            return None
        return cast("UiSpec | None", spec)

    def llm_defaults(self, kind: str, name: str) -> dict[str, str] | None:
        """The ``prompt`` and ``guard`` a plugin ships, if it ``needs_llm``; else None. The console
        shows them locked until the owner overrides them."""
        rec = self._records.get((kind, name))
        inst = rec.instance if rec else None
        if inst is None or not getattr(inst, "needs_llm", False):
            return None
        return {"prompt": str(getattr(inst, "prompt", "")), "guard": str(getattr(inst, "guard", ""))}

    def config_schema(self, kind: str, name: str) -> dict[str, Any] | None:
        """JSON Schema of a plugin's settings, if it declares ``config_model`` (pydantic)."""
        rec = self._records.get((kind, name))
        model = getattr(rec.instance, "config_model", None) if rec else None
        schema = getattr(model, "model_json_schema", None)
        return schema() if callable(schema) else None

    @staticmethod
    def _validate_config(instance: Any, config: dict[str, Any]) -> None:
        """Validate ``config`` against a plugin's ``config_model`` before it starts, so a bad table
        is a clear field-level message instead of a late failure. No ``config_model``: unaffected."""
        model = getattr(instance, "config_model", None)
        validate = getattr(model, "model_validate", None)
        if not callable(validate):
            return
        try:
            validate(config)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'config'}: {e['msg']}"
                for e in exc.errors(include_input=False, include_url=False)[:5]
            )
            raise PluginError(f"invalid [plugins] config ({problems})") from None

    # -- lookup ------------------------------------------------------------------------------
    def statuses(self) -> list[PluginStatus]:
        return [
            PluginStatus(
                kind=r.kind, name=r.name, plugin=r.plugin, version=r.info.version if r.info else "",
                started=r.started, healthy=r.healthy, detail=r.detail,
                enabled=not r.disabled and self._enabled_by_table(r),
                enabled_default=self._enabled_default(r.instance),
            )  # fmt: skip
            for r in sorted(self._records.values(), key=lambda r: (r.kind, r.name))
        ]

    def provider_of(self, name: str) -> str:
        """The dotted path of the class behind the plugin instance ``name`` (``pkg.module.Class``), or its
        entry point's name when nothing has been instantiated. Several kinds may share a name; the first wins."""
        for r in sorted(self._records.values(), key=lambda r: (r.kind, r.name)):
            if r.name == name:
                cls = type(r.instance) if r.instance is not None else None
                return f"{cls.__module__}.{cls.__qualname__}" if cls else r.plugin
        return ""

    def usable(self, kind: str) -> dict[str, Any]:
        """Every started, healthy plugin of ``kind`` by instance name, in name order."""
        return {
            r.name: r.instance
            for r in sorted(self._records.values(), key=lambda r: r.name)
            if r.kind == kind and r.started and r.healthy and r.instance is not None
        }

    def get(self, kind: str, name: str | None = None) -> Any:
        """The started, healthy plugin of ``kind`` called ``name`` (default: the active one)."""
        name = name or self._active_name(kind)
        if name is None:
            raise PluginError(f"give the name of the {kind} to use")
        rec = self._records.get((kind, name))
        if rec is None:
            known = ", ".join(sorted(n for k, n in self._records if k == kind)) or "none installed"
            raise NotFound(f"no {kind} named {name!r} (found: {known})")
        if not (rec.started and rec.healthy):
            raise PluginError(f"{kind} {name!r} is unhealthy: {rec.detail or 'not started'}")
        return rec.instance

    def named_triagers(self) -> dict[str, Any]:
        """Every healthy triager by name, in ``rank`` order (then name): the order they run."""
        usable = self.usable("triager")
        order = sorted(usable, key=lambda n: (getattr(usable[n], "rank", 50), n))
        return {n: usable[n] for n in order}

    def agent_eligible_kinds(self) -> list[str]:
        """Job kinds an external agent could take: the analyzers that need a model and do not need
        the vector index (an agent cannot reach it), as ``<kind>.<name>``. The owner opts in with
        ``[swarm] allowed_kinds``; this only lists."""
        return [
            f"{r.kind}.{r.name}"
            for r in self._shared()
            if self.kinds.get(r.kind).analyzer
            and getattr(r.instance, "needs_llm", False)
            and not getattr(r.instance, "needs_vec", False)
        ]

    def needs_vec_kinds(self) -> list[str]:
        """The analyzers that use the embedder and vector store, as ``<kind>.<name>``: their jobs
        run in core and wait for a healthy embedder and vector store."""
        return [
            f"{r.kind}.{r.name}"
            for r in self._shared()
            if self.kinds.get(r.kind).analyzer and getattr(r.instance, "needs_vec", False)
        ]
