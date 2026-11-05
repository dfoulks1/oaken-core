# SPDX-License-Identifier: Apache-2.0
"""Intake inside the daemon: a provider submits, a triager decides, an ingestor stores."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from oaken_core.clock import FakeClock
from oaken_core.daemon.core import Core
from oaken_core.ids import SubjectId
from oaken_core.intake import KIND, IntakeItem, Verdict
from oaken_core.plugins.base import BaseIngestor, BasePlugin, BaseTriager
from oaken_core.plugins.host import PluginTables
from oaken_core.settings import Settings
from oaken_core.store import SqliteStore

from .test_host import EP


class Rules(BaseTriager):
    rank: ClassVar[int] = 10

    async def triage(self, item: IntakeItem) -> Verdict | None:
        if "junk" in str(item.body):
            return Verdict("reject", "junk")
        return Verdict("accept", "fine", {"priority": "high"})


class Shelf(BaseIngestor):
    stored: ClassVar[list[IntakeItem]] = []

    async def ingest(self, item: IntakeItem) -> SubjectId:
        self.stored.append(item)
        return SubjectId(f"real-{len(self.stored)}")


class Feed(BasePlugin):  # a provider: it only pushes items in
    pass


async def make(tmp_path: Path, clock: FakeClock, **tables: Any) -> Core:
    found = {"oaken_core.stores": [EP("sqlite", SqliteStore)], "oaken_core.triagers": [EP("rules", Rules)],
             "oaken_core.ingestors": [EP("shelf", Shelf)], "oaken_core.providers": [EP("feed", Feed)]}  # fmt: skip
    cfg = {"store": {"sqlite": {"path": str(tmp_path / "core.db")}}, **tables}
    core = Core(Settings(plugins=PluginTables(tables=cfg)), clock=clock, discover=lambda g: found.get(g, []))
    await core.open()
    await core.host.start()
    return core


@pytest.fixture
async def core(tmp_path: Path) -> AsyncIterator[Core]:
    Shelf.stored.clear()
    c = await make(tmp_path, FakeClock())
    try:
        yield c
    finally:
        await c.host.stop()


def submit(core: Core) -> Any:
    ctx = core.host.get("provider", "feed").ctx
    assert ctx.submit is not None
    return ctx.submit


async def test_a_provider_submits_the_triager_accepts_and_the_ingestor_stores(core: Core) -> None:
    prov = await submit(core)("feed", {"text": "hello"}, labels={"confidential": "true"})
    await core.runner.drain()
    assert [i.body for i in Shelf.stored] == [{"text": "hello"}]
    assert await core.host.label_store.all("real-1") == {"priority": "high", "confidential": "true"}
    assert await core.host.label_store.all(prov) == {}  # the labels moved to the real subject


async def test_a_reject_never_reaches_the_ingestor(core: Core) -> None:
    await submit(core)("feed", {"text": "junk"})
    await core.runner.drain()
    assert Shelf.stored == []
    (job,) = await core.runner.queue.query(kind=KIND)
    assert job.status == "succeeded"


async def test_with_no_ingestor_an_accepted_item_waits_without_failing_and_flows_once_one_starts(tmp_path: Path) -> None:
    Shelf.stored.clear()
    clock = FakeClock()
    core = await make(tmp_path, clock)
    try:
        await core.host.disable("shelf")
        await submit(core)("feed", {"text": "hello"})
        await core.runner.drain()
        (job,) = await core.runner.queue.query(kind=KIND)
        assert job.status == "queued" and job.attempts == 0 and "no ingestor" in (job.last_error or "")
        await core.host.enable("shelf")
        clock.advance(seconds=core.settings.router.wait_seconds + 1)
        await core.runner.drain()
        assert len(Shelf.stored) == 1
    finally:
        await core.host.stop()


async def test_a_triager_that_stops_counts_from_the_next_item(core: Core) -> None:
    await core.host.disable("rules")
    await submit(core)("feed", {"text": "junk"})  # nobody left to call it junk: unsure, so it is stored
    await core.runner.drain()
    assert len(Shelf.stored) == 1
