# SPDX-License-Identifier: Apache-2.0
"""What an operation handler may reach: the services the daemon (or a test rig) has built.

Replaces oaken-mail's mail-shaped ``Services``. The core ones are required; the rest are optional
because a process may run without them. A handler that needs one asks with ``need`` and the caller
gets ``unavailable`` rather than a crash.
"""

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, TypeVar

from oaken_core.agents import AgentRegistry
from oaken_core.changelog import ChangeLog
from oaken_core.clock import Clock
from oaken_core.db import Database
from oaken_core.errors import Unavailable
from oaken_core.events import EventBus
from oaken_core.jobs import JobRunner, Jobs
from oaken_core.labels import LabelStore
from oaken_core.plugins.host import PluginHost
from oaken_core.queues import Queues
from oaken_core.scheduler import Scheduler
from oaken_core.settings import Settings
from oaken_core.trace import Tracer

T = TypeVar("T")


def need(value: T | None, what: str) -> T:
    if value is None:
        raise Unavailable(f"{what} is not running")
    return value


@dataclass
class Services:
    db: Database
    clock: Clock
    settings: Settings
    queues: Queues
    jobs: Jobs
    labels: LabelStore
    changelog: ChangeLog
    bus: EventBus | None = None
    agents: AgentRegistry | None = None
    host: PluginHost | None = None
    runner: JobRunner | None = None  # what to do with a result an agent made (``adopt``)
    scheduler: Scheduler | None = None
    tracer: Tracer | None = None
    router: Any = None  # oaken_core.router.Router
    eventlog: Any = None  # oaken_core.eventlog.EventLog (None: ``[log] events_file`` is empty)
    queues_from_config: bool = True  # False: the product fixes its own queues; none can be added
    intake: Any = None  # oaken_core.intake.Intake: the front door
    secrets: Any = None  # a ``SecretStore`` (the OS keyring); None where there is none
    reload: Callable[[], None] | None = None  # the daemon's reload (None: core was not started by it)

    def need_eventlog(self) -> Any:
        if self.eventlog is None:
            raise Unavailable("the event log is off: set [log] events_file to turn it on")
        return self.eventlog

    @property
    def version(self) -> str:
        return self.settings.version
