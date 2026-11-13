# SPDX-License-Identifier: Apache-2.0
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from oaken_core.changelog import ChangeLog
from oaken_core.clock import FakeClock
from oaken_core.db import Database
from oaken_core.jobs import JobRunner, Jobs
from oaken_core.labels import LabelMaker, LabelStore
from oaken_core.queues import Queues
from oaken_core.settings import JobsSettings

if TYPE_CHECKING:
    from oaken_core.agents import AgentRegistry
    from oaken_core.operations.client import CoreClient
    from oaken_core.operations.registry import OperationRegistry
    from oaken_core.operations.services import Services


@pytest.fixture(autouse=True)
def oaken_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test runs with all paths relocated under tmp_path; env is otherwise clean."""
    import os

    for key in [k for k in os.environ if k.startswith("OAKEN_")]:
        monkeypatch.delenv(key)
    home = tmp_path / "home"
    monkeypatch.setenv("OAKEN_HOME", str(home))
    monkeypatch.setenv("OAKEN_STATE_DIR", str(tmp_path / "state"))
    return home


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    async with await Database.open(tmp_path / "t.db", read_pool_size=3) as database:
        yield database


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def core_db(tmp_path: Path) -> AsyncIterator[Database]:
    """A database with core's migrations applied, opened through the default store."""
    from oaken_core.store import SqliteStore, open_core

    database = await open_core(SqliteStore(tmp_path / "core.db", read_pool_size=2))
    try:
        yield database
    finally:
        await database.close()


@dataclass
class Kit:
    db: Database
    clock: FakeClock
    queues: Queues
    maker: LabelMaker
    labels: LabelStore
    log: ChangeLog
    jobs: Jobs
    runner: JobRunner


@pytest.fixture
async def kit(core_db: Database, clock: FakeClock) -> Kit:
    """Core wired together: queues, labels (with the real-queue check), log, job queue, runner."""
    queues = Queues(default_workers=1)
    maker = LabelMaker.core()
    maker.set_validator("queue", queues.validate_label)
    log = ChangeLog(clock)
    labels = LabelStore(core_db, maker, log, clock)
    settings = JobsSettings(backoff_jitter=0, backoff_base_seconds=10, lease_seconds=30)
    jobs = Jobs(core_db, clock, queues, labels=labels, changelog=log, settings=settings)
    return Kit(core_db, clock, queues, maker, labels, log, jobs, JobRunner(jobs))


@dataclass
class Ops:
    kit: Kit
    svc: "Services"
    client: "CoreClient"
    registry: "OperationRegistry"
    agents: "AgentRegistry"


@pytest.fixture
async def ops(kit: Kit) -> Ops:
    """The operation layer wired over the kit: services, the core registry, the in-process client."""
    from oaken_core.agents import AgentRegistry
    from oaken_core.operations.builtin import core_registry
    from oaken_core.operations.client import CoreClient
    from oaken_core.operations.services import Services
    from oaken_core.settings import Settings, SwarmSettings

    swarm = SwarmSettings(enabled=True, allowed_kinds=["work"], default_max_leases=2, agent_lease_seconds=300)
    kit.jobs.swarm = swarm
    agents = AgentRegistry(kit.db, kit.clock, swarm)
    svc = Services(db=kit.db, clock=kit.clock, settings=Settings(swarm=swarm, version="9.9.9"),
                   queues=kit.queues, jobs=kit.jobs, labels=kit.labels, changelog=kit.log, agents=agents)  # fmt: skip
    registry = core_registry()
    return Ops(kit, svc, CoreClient(registry, svc), registry, agents)
