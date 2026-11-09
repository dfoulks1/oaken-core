# SPDX-License-Identifier: Apache-2.0
"""The composition root: builds core's object graph from ``Settings`` and runs it as units.

``open`` builds (the store first: everything needs it). ``start`` brings the units up in order and
``stop`` takes them down in reverse. Plugin operations, job kinds and schedules are attached once
the host has loaded them, so only plugins that started contribute. Rewritten for core (oaken-mail's
``daemon/core.py`` is mail-shaped).
"""

import asyncio
from collections.abc import Callable
from typing import Any

from oaken_core import __version__, paths
from oaken_core.agents import AgentRegistry
from oaken_core.api import Api
from oaken_core.api.mcp_server import McpServer
from oaken_core.api.runtime import MCP_TOKEN_FILE, TOKEN_FILE, load_or_create_token
from oaken_core.api.server import ApiServer
from oaken_core.changelog import ChangeLog
from oaken_core.clock import Clock, SystemClock
from oaken_core.console import Console, ConsoleServer
from oaken_core.daemon.units import Lifecycle, ServiceUnit, unit
from oaken_core.errors import ConfigError, Deferred, NotFound, PluginError, Unavailable
from oaken_core.events import EventBus
from oaken_core.intake import Intake, IntakeItem
from oaken_core.jobs import Jobs, JobRunner
from oaken_core.ids import SubjectId
from oaken_core.labels import (
    BaseLabel,
    LabelError,
    LabelMaker,
    LabelStore,
    ReservedLabel,
)
from oaken_core.logging import get_logger
from oaken_core.operations.builtin import core_registry
from oaken_core.operations.client import CoreClient
from oaken_core.operations.services import Services
from oaken_core.plugins.kinds import check_queue_kind
from oaken_core.eventlog import EventLog, event_log_path
from oaken_core.maintenance import register_maintenance_jobs
from oaken_core.secrets import KeyringSecrets, SecretStore
from oaken_core.plugins.host import Discover, PluginHost, discover_entry_points
from oaken_core.plugins.jobs import register_plugin_jobs, seed_plugin_schedules
from oaken_core.plugins.ops import plugin_operations
from oaken_core.logstore import LogTable, LogToBus
from oaken_core.queues import DEFAULT, Queues
from oaken_core.router import HostDirectory, Router
from oaken_core.scheduler import Scheduler
from oaken_core.parts import Part
from oaken_core.stagerun import StagePayload, StageRunner
from oaken_core.settings import Settings
from oaken_core.trace import Tracer

log = get_logger("oaken_core.daemon")


class _Triager:
    """A plugin triager as intake wants it: with the name the host knows it by."""

    def __init__(self, name: str, plugin: Any) -> None:
        self.name, self.plugin = name, plugin

    @property
    def rank(self) -> int:
        return int(self.plugin.rank)

    async def triage(self, item: IntakeItem) -> Any:
        return await self.plugin.triage(item)


JOB_GRACE = (
    30.0  # seconds running jobs get to finish at stop; then their leases are released
)


class Core:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        clock: Clock | None = None,
        discover: Discover = discover_entry_points,
        queues_from_config: bool = True,
        secrets: SecretStore | None = None,
    ) -> None:
        self.settings = (settings or Settings()).model_copy(
            update={"version": __version__}
        )
        self.clock = clock or SystemClock()
        self._discover = discover
        self.secrets: SecretStore = secrets or KeyringSecrets()
        self.queues_from_config = (
            queues_from_config  # a product that fixes its own queues turns this off
        )
        self.started: list[str] = []  # names of the units that are up, in order
        self._life: Lifecycle | None = None
        self._serving: asyncio.Task[None] | None = None
        self._console_task: asyncio.Task[None] | None = None
        self.reload_hook: Callable[
            [], None
        ] | None = None  # armed by the daemon (``serve``)

    @property
    def port(self) -> int:
        return self.server.port

    @property
    def console_port(self) -> int:
        return self.console_server.port

    async def open(self) -> None:
        """Build the graph. Opens the database; ``stop`` closes it."""
        s, clock = self.settings, self.clock
        self.bus = EventBus()
        self.logbus = (
            LogToBus(self.bus) if s.log.enabled else None
        )  # every log line goes on the bus; a table keeps them unless a file does
        self.logtable = (
            LogTable(self.bus, s.log.keep_days)
            if s.log.enabled and not s.log.file
            else None
        )
        if self.logbus is not None:
            self.logbus.attach()
        if self.logtable is not None:
            self.logtable.start()
        self.host = PluginHost(
            s.plugins,
            clock,
            self.bus,
            discover=self._discover,
            extensions_dirs=s.extensions.dirs,
            core_version=__version__,
        )
        self.host.secrets = self.secrets
        db = await self.host.start_store()
        if self.logtable is not None:
            self.logtable.bind(db)
        queues = self._make_queues()
        maker = LabelMaker.core()
        maker.set_validator("queue", queues.validate_label)
        for key, t in s.labels.items():
            try:
                maker.register_config(
                    (ReservedLabel if t.reserved else BaseLabel).new(
                        key, t.description, tuple(t.values), tuple(t.writers)
                    )
                )
            except LabelError as exc:
                raise ConfigError(f"[labels.{key}]: {exc.message}") from None
        changelog = ChangeLog(clock)
        labels = LabelStore(db, maker, changelog, clock)
        self.host.attach_labels(labels, maker)
        jobs = Jobs(
            db,
            clock,
            queues,
            labels=labels,
            changelog=changelog,
            settings=s.jobs,
            swarm=s.swarm,
            bus=self.bus,
        )
        self.agents = AgentRegistry(db, clock, s.swarm, self.bus)
        self.scheduler = Scheduler(db, jobs, clock)
        self.tracer = Tracer(
            paths.data_dir() / "trace.jsonl",
            clock,
            max_bytes=s.log.file_max_bytes,
            backups=s.log.file_backups,
            enabled=s.log.trace,
        )
        self.tracer.bus = self.bus
        self.host.tracer = self.tracer
        self.router = Router(db, clock, s.router, HostDirectory(self.host, s.router))
        self.host.router = self.router
        self.runner = JobRunner(jobs)
        register_maintenance_jobs(self.runner, db)
        self.eventlog = (
            EventLog(
                event_log_path(s.log.events_file),
                max_bytes=s.log.file_max_bytes,
                backups=s.log.file_backups,
            )
            if s.log.events_file
            else None
        )
        self.intake = Intake(
            jobs,
            labels,
            db,
            changelog,
            triager_source=self._triagers,
            formatter_for=self._formatter,
            ingest=self._ingest,
            router=self.router,
        )
        self.intake.register(self.runner)
        self.stages = StageRunner(jobs, labels, enrichers_for=lambda st: self._in_stage("enricher", st),
                                  actions_for=self._actions_for, store=lambda: self.host.store)  # fmt: skip
        self.stages.register(self.runner)
        self.intake.on_stored = self.stages.begin
        self.intake.rerun_hook = self._rerun
        self.host.intake = self.intake
        svc = Services(db=db, clock=clock, settings=s, queues=queues, jobs=jobs, labels=labels, changelog=changelog,
                       bus=self.bus, agents=self.agents, host=self.host, secrets=self.secrets, intake=self.intake, queues_from_config=self.queues_from_config, eventlog=self.eventlog, scheduler=self.scheduler, tracer=self.tracer, router=self.router, reload=self.request_reload)  # fmt: skip
        self.registry = core_registry()
        self.registry.attach_plugins(lambda: plugin_operations(self.host))
        self.client = CoreClient(self.registry, svc)
        self.api = Api(
            self.client,
            token=load_or_create_token(paths.config_dir() / TOKEN_FILE),
            started_at=clock.now(),
        )
        self.person = McpServer(
            self.client, token=load_or_create_token(paths.config_dir() / MCP_TOKEN_FILE)
        )
        self.agent_door = McpServer(self.client, agents=self.agents)
        self.api.mount(self.person.surface, self.person)
        self.api.mount(self.agent_door.surface, self.agent_door)
        self.server = ApiServer(self.api)
        self.console = Console(
            self.client, svc
        )  # core's own: no plugin can switch it off
        self.console_server = ConsoleServer(self.console, s.console)
        self._life = Lifecycle(self._units())

    def _make_queues(self) -> Queues:
        """``default``, then the ``[queues.<name>]`` tables in the order the file lists them. Each serves one
        analyzer kind that is registered; a product that fixes its own queues refuses the tables."""
        queues = Queues()
        tables = self.settings.queues
        if tables and not self.queues_from_config:
            raise ConfigError(
                "this product defines its own queues: remove [queues.*] from config.toml"
            )
        for name, q in tables.items():
            if (
                name == DEFAULT
            ):  # Intake: its table only says which plugins are off there
                if q.kind or q.label or q.description or q.workers != 1:
                    raise ConfigError(
                        "[queues.default] already exists: Intake is always there, and its table may only set `disabled`"
                    )
                queues.set_disabled(DEFAULT, tuple(q.disabled))
                continue
            check_queue_kind(self.host.kinds, name, q.kind)
            queues.new_queue(
                name,
                q.description,
                q.workers,
                kind=q.kind,
                label=q.label,
                disabled=tuple(q.disabled),
            )
        return queues

    def request_reload(self) -> None:
        """Ask the daemon to rebuild core on ``config.toml``. Half a second later, so the page that
        asked is answered before the console it came from is stopped."""
        if self.reload_hook is None:
            raise Unavailable(
                "core was not started by the daemon, so there is nothing to reload it"
            )
        asyncio.get_running_loop().call_later(0.5, self.reload_hook)

    def _triagers(self, stage: str) -> list[_Triager]:
        """The healthy triagers of one stage right now, so one that starts or stops counts from the next item.
        A triager runs in the stage its ``stage`` setting names, else in the first triager stage."""
        queues = self.runner.jobs.queues
        first = next((q.name for q in queues.all() if q.kind == "triager"), None)
        off = (
            queues.get(stage).disabled
            if stage in [q.name for q in queues.all()]
            else ()
        )
        return [_Triager(name, plugin) for name, plugin in self.host.named_triagers().items()
                if name not in off and (self.host.tables.table("triager", name).get("stage") or first) in (stage, None)]  # fmt: skip

    def _in_stage(self, kind: str, stage: str) -> list[tuple[str, Any]]:
        """The healthy plugins of an analyzer ``kind`` that run in ``stage`` (their ``stage`` setting, else the first
        stage of that kind), by name, minus any switched off there."""
        queues = self.runner.jobs.queues
        first = next((q.name for q in queues.all() if q.kind == kind), None)
        off = queues.get(stage).disabled if stage in [q.name for q in queues.all()] else ()
        return [(n, p) for n, p in self.host.usable(kind).items()
                if n not in off and (self.host.tables.table(kind, n).get("stage") or first) in (stage, None)]  # fmt: skip

    def _actions_for(self, route: str) -> list[tuple[str, Any, str | None]]:
        """The healthy actions answering ``route``, each with the stage it runs in (None: no action stage made)."""
        queues = self.runner.jobs.queues
        first = next((q.name for q in queues.all() if q.kind == "action"), None)
        return [(n, p, self.host.tables.table("action", n).get("stage") or first)
                for n, p in self.host.usable("action").items() if route in getattr(p, "routes", ())
                and n not in (queues.get(st).disabled if (st := self.host.tables.table("action", n).get("stage") or first) else ())]  # fmt: skip

    def _formatter(self, fmt: str | None) -> Any:
        """The healthy formatter for an item's ``format`` (a format it declares, or its own name), or None."""
        if not fmt:
            return None
        off = self.runner.jobs.queues.get(DEFAULT).disabled  # switched off in Intake
        found = {n: p for n, p in self.host.usable("formatter").items() if n not in off}
        return next(
            (
                p
                for n, p in found.items()
                if fmt == n or fmt in getattr(p, "formats", ())
            ),
            None,
        )

    async def _rerun(self, subject: str, source: str, stage: str, routes: list[str]) -> None:
        """Rebuild a stored item from what its ingestor kept, and hand it to a later stage."""
        try:
            ingestor = self.host.get("ingestor")
            parts = await ingestor.read(subject)
        except (PluginError, NotFound, AttributeError) as exc:
            raise Unavailable(f"cannot read {subject!r} back from the ingestor ({exc})") from None
        p = StagePayload(subject_id=subject, source=source, labels=await self.intake.labels.all(subject),
                         parts={n: Part(name=n, content=c) for n, c in parts.items()})  # fmt: skip
        await self.stages.rerun(p, stage, routes)

    async def _ingest(self, item: IntakeItem) -> SubjectId:
        """Hand an accepted item to the active ingestor. With none, the item waits in intake (it is
        deferred, not failed): core has no nouns, so a product plugin has to say where items go."""
        try:
            ingestor = self.host.get("ingestor")
        except (PluginError, NotFound) as exc:
            raise Deferred(
                f"no ingestor is running ({exc})", self.settings.router.wait_seconds
            ) from None
        return SubjectId(str(await ingestor.ingest(item)))

    def _units(self) -> list[ServiceUnit]:
        async def none() -> None:
            return None

        async def start_host() -> None:
            await self.host.start()
            register_plugin_jobs(self.host, self.runner)
            await seed_plugin_schedules(self.host, self.scheduler)
            await self.client.exposure.seed(
                self.settings.operations.mcp_allowed, self.registry
            )

        async def stop_jobs() -> None:
            await self.runner.stop(JOB_GRACE)

        async def start_mcp() -> None:
            await self.person.start()
            await self.agent_door.start()

        async def stop_mcp() -> None:
            await self.agent_door.stop()
            await self.person.stop()

        async def start_api() -> None:
            self._serving = asyncio.create_task(self._serve(), name="oaken-api")
            while not self.server.started:
                if self._serving.done():
                    self._serving.result()  # the reason it died (a port taken, say)
                    raise Unavailable("the API server stopped while starting")
                await asyncio.sleep(0.01)
            self.api.bind(self.server.port)
            self.api.start()

        async def stop_api() -> None:
            self.api.stop()
            self.server.stop()
            serving, self._serving = self._serving, None
            if serving is not None:
                await serving

        async def start_console() -> None:
            self._console_task = asyncio.create_task(
                self._serve_console(), name="oaken-console"
            )
            while not self.console_server.started:
                if self._console_task.done():
                    self._console_task.result()
                    raise Unavailable("the console stopped while starting")
                await asyncio.sleep(0.01)
            self.console.bind(self.console_server.port)

        async def stop_console() -> None:
            self.console_server.stop()
            task, self._console_task = self._console_task, None
            if task is not None:
                await task

        async def stop_trace() -> None:
            self.tracer.close()

        async def stop_scheduler() -> None:
            await self.scheduler.stop()

        async def start_scheduler() -> None:
            self.scheduler.start()

        async def start_jobs() -> None:
            self.runner.start()

        async def start_events() -> None:
            if self.eventlog is not None:
                self._detach_eventlog = self.eventlog.attach(self.bus)

        async def stop_events() -> None:
            detach = getattr(self, "_detach_eventlog", None)
            if detach is not None:
                detach()
            if self.eventlog is not None:
                self.eventlog.close()

        return [
            unit(
                "events", start_events, stop_events
            ),  # first, so the plugins' start-up is in the record
            unit("trace", none, stop_trace),
            unit(
                "console", start_console, stop_console
            ),  # before the host: a plugin that fails to start cannot take it down
            unit(
                "host", start_host, self.host.stop
            ),  # last to stop: the others write on the way out
            unit("scheduler", start_scheduler, stop_scheduler),
            unit("jobs", start_jobs, stop_jobs),
            unit("mcp", start_mcp, stop_mcp),
            unit("api", start_api, stop_api),
        ]

    async def _serve(self) -> None:
        try:
            await self.server.serve()
        except SystemExit:  # uvicorn exits, rather than raises, when it cannot bind
            raise Unavailable(
                f"the API server could not listen on {self.settings.api.host}:{self.settings.api.port}"
            ) from None

    async def _serve_console(self) -> None:
        try:
            await self.console_server.serve()
        except SystemExit:
            raise Unavailable(
                f"the console could not listen on 127.0.0.1:{self.settings.console.port}"
            ) from None

    async def start(self) -> int:
        """Bring every unit up and return the port the API listens on."""
        assert self._life is not None, "open() first"
        try:
            await self._life.start(self.started)
        except BaseException:
            await self.host.stop()  # the store opened in open() even though no unit owned it yet
            raise
        return self.port

    async def stop(self) -> None:
        if self._life is not None:
            await self._life.stop(self.started)
        if self.logbus is not None:
            self.logbus.detach()
            self.logbus = None
        if self.logtable is not None:
            await self.logtable.stop()  # flush what is queued while the database is still open
            self.logtable = None
        await self.host.stop()  # a no-op once its unit has run; closes the store otherwise
