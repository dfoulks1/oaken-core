# SPDX-License-Identifier: Apache-2.0
"""The composition root: builds core's object graph from ``Settings`` and runs it as units.

``open`` builds (the store first: everything needs it). ``start`` brings the units up in order and
``stop`` takes them down in reverse. Plugin operations, job kinds and schedules are attached once
the host has loaded them, so only plugins that started contribute. Rewritten for core (oaken-mail's
``daemon/core.py`` is mail-shaped).
"""

import asyncio

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
from oaken_core.errors import Unavailable
from oaken_core.events import EventBus
from oaken_core.jobs import JobQueue, JobRunner
from oaken_core.labels import LabelMaker, LabelStore
from oaken_core.logging import get_logger
from oaken_core.operations.builtin import core_registry
from oaken_core.operations.client import CoreClient
from oaken_core.operations.services import Services
from oaken_core.plugins.host import Discover, PluginHost, discover_entry_points
from oaken_core.plugins.jobs import register_plugin_jobs, seed_plugin_schedules
from oaken_core.plugins.ops import plugin_operations
from oaken_core.queues import Queues
from oaken_core.router import HostDirectory, Router
from oaken_core.scheduler import Scheduler
from oaken_core.settings import Settings
from oaken_core.trace import Tracer

log = get_logger("oaken_core.daemon")

JOB_GRACE = 30.0  # seconds running jobs get to finish at stop; then their leases are released


class Core:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        clock: Clock | None = None,
        discover: Discover = discover_entry_points,
    ) -> None:
        self.settings = (settings or Settings()).model_copy(update={"version": __version__})
        self.clock = clock or SystemClock()
        self._discover = discover
        self.started: list[str] = []  # names of the units that are up, in order
        self._life: Lifecycle | None = None
        self._serving: asyncio.Task[None] | None = None
        self._console_task: asyncio.Task[None] | None = None

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
        self.host = PluginHost(s.plugins, clock, self.bus, discover=self._discover, core_version=__version__)
        db = await self.host.start_store()
        queues = Queues()
        maker = LabelMaker.core()
        maker.set_validator("queue", queues.validate_label)
        changelog = ChangeLog(clock)
        labels = LabelStore(db, maker, changelog, clock)
        self.host.attach_labels(labels, maker)
        queue = JobQueue(db, clock, queues, labels=labels, changelog=changelog, settings=s.jobs, swarm=s.swarm, bus=self.bus)
        self.agents = AgentRegistry(db, clock, s.swarm, self.bus)
        self.scheduler = Scheduler(db, queue, clock)
        self.runner = JobRunner(queue)
        self.tracer = Tracer(paths.data_dir() / "trace.jsonl", clock, max_bytes=s.log.file_max_bytes, backups=s.log.file_backups)
        self.tracer.bus = self.bus
        self.host.tracer = self.tracer
        self.router = Router(db, clock, s.router, HostDirectory(self.host, s.router))
        self.host.router = self.router
        svc = Services(db=db, clock=clock, settings=s, queues=queues, queue=queue, labels=labels, changelog=changelog,
                       bus=self.bus, agents=self.agents, host=self.host, scheduler=self.scheduler, tracer=self.tracer, router=self.router)  # fmt: skip
        self.registry = core_registry()
        self.registry.attach_plugins(lambda: plugin_operations(self.host))
        self.client = CoreClient(self.registry, svc)
        self.api = Api(self.client, token=load_or_create_token(paths.config_dir() / TOKEN_FILE), started_at=clock.now())
        self.person = McpServer(self.client, token=load_or_create_token(paths.config_dir() / MCP_TOKEN_FILE))
        self.agent_door = McpServer(self.client, agents=self.agents)
        self.api.mount(self.person.surface, self.person)
        self.api.mount(self.agent_door.surface, self.agent_door)
        self.server = ApiServer(self.api)
        self.console = Console(self.client, svc)  # core's own: no plugin can switch it off
        self.console_server = ConsoleServer(self.console, s.console)
        self._life = Lifecycle(self._units())

    def _units(self) -> list[ServiceUnit]:
        async def none() -> None:
            return None

        async def start_host() -> None:
            await self.host.start()
            register_plugin_jobs(self.host, self.runner)
            await seed_plugin_schedules(self.host, self.scheduler)
            await self.client.exposure.seed(self.settings.operations.mcp_allowed, self.registry)

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
            self._console_task = asyncio.create_task(self._serve_console(), name="oaken-console")
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

        return [
            unit("trace", none, stop_trace),
            unit("console", start_console, stop_console),  # before the host: a plugin that fails to start cannot take it down
            unit("host", start_host, self.host.stop),  # last to stop: the others write on the way out
            unit("scheduler", start_scheduler, stop_scheduler),
            unit("jobs", start_jobs, stop_jobs),
            unit("mcp", start_mcp, stop_mcp),
            unit("api", start_api, stop_api),
        ]

    async def _serve(self) -> None:
        try:
            await self.server.serve()
        except SystemExit:  # uvicorn exits, rather than raises, when it cannot bind
            raise Unavailable(f"the API server could not listen on {self.settings.api.host}:{self.settings.api.port}") from None

    async def _serve_console(self) -> None:
        try:
            await self.console_server.serve()
        except SystemExit:
            raise Unavailable(f"the console could not listen on 127.0.0.1:{self.settings.console.port}") from None

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
        await self.host.stop()  # a no-op once its unit has run; closes the store otherwise
