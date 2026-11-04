# SPDX-License-Identifier: Apache-2.0
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from oaken_core.changelog import ChangeLog
from oaken_core.clock import FakeClock
from oaken_core.db import Database
from oaken_core.jobs import JobQueue, JobRunner
from oaken_core.labels import LabelMaker, LabelStore
from oaken_core.queues import Queues
from oaken_core.settings import JobsSettings


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
    queue: JobQueue
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
    queue = JobQueue(core_db, clock, queues, labels=labels, changelog=log, settings=settings)
    return Kit(core_db, clock, queues, maker, labels, log, queue, JobRunner(queue))
