# SPDX-License-Identifier: Apache-2.0
"""Plain `oaken` can take an item: submit it, triage it, store it in the built-in ingestor, read it back."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from oaken_core.clock import FakeClock
from oaken_core.daemon.core import Core
from oaken_core.ids import SubjectId
from oaken_core.intake import IntakeItem, Verdict
from oaken_core.items import ItemsIngestor
from oaken_core.operations.caller import CallerContext
from oaken_core.plugins.base import BaseIngestor, BaseTriager
from oaken_core.plugins.host import PluginTables
from oaken_core.settings import Settings
from oaken_core.store import SqliteStore

from .test_host import EP

CLI = CallerContext.cli()


class Junk(BaseTriager):
    rank: ClassVar[int] = 10

    async def triage(self, item: IntakeItem) -> Verdict | None:
        return Verdict("reject", "junk") if "junk" in str(item.body) else None


async def make(tmp_path: Path, extra: dict[str, list[Any]] | None = None) -> Core:
    found = {"oaken_core.stores": [EP("sqlite", SqliteStore)], "oaken_core.ingestors": [EP("items", ItemsIngestor)],
             "oaken_core.triagers": [EP("junk", Junk)], **(extra or {})}  # fmt: skip
    tables = {"store": {"sqlite": {"path": str(tmp_path / "core.db")}}}
    core = Core(Settings(plugins=PluginTables(tables=tables)), clock=FakeClock(), discover=lambda g: found.get(g, []))
    await core.open()
    await core.host.start()
    return core


@pytest.fixture
async def core(tmp_path: Path) -> AsyncIterator[Core]:
    c = await make(tmp_path)
    try:
        yield c
    finally:
        await c.host.stop()


async def test_a_person_submits_an_item_and_it_is_triaged_stored_and_listed(core: Core) -> None:
    call = core.client.call
    args = {"source": "notes", "body": "buy milk"}
    asked = await call("intake_submit", args, CLI)
    assert asked.proposal is not None and "notes" in asked.proposal.summary and await core.runner.drain() == 0  # nothing yet
    out = (await call("intake_submit", args, CLI, confirm=True)).output
    assert out.subject_id  # type: ignore[union-attr]
    assert await core.runner.drain() == 3  # format, triage, then the ingestor stores it
    listed = (await call("items.list", {}, CLI)).output
    assert listed.total == 1 and listed.items[0].source == "notes" and "buy milk" in listed.items[0].preview  # type: ignore[union-attr]
    assert listed.items[0].subject_id != out.subject_id  # type: ignore[union-attr]  # the real id, not the provisional one


async def test_a_rejected_item_is_dropped_and_never_stored(core: Core) -> None:
    await core.client.call("intake_submit", {"source": "mail", "body": "total junk"}, CLI, confirm=True)
    await core.runner.drain()
    assert (await core.client.call("items.list", {}, CLI)).output.total == 0  # type: ignore[union-attr]


async def test_an_item_waits_when_no_ingestor_runs_and_flows_when_one_starts(tmp_path: Path) -> None:
    c = await make(tmp_path, {"oaken_core.ingestors": []})
    try:
        await c.client.call("intake_submit", {"source": "a", "body": "x"}, CLI, confirm=True)
        await c.runner.drain()
        stats = {r.queue: r for r in await c.client.svc.jobs.stats()}
        assert stats["store"].queued == 1  # deferred, not failed: nothing was lost
    finally:
        await c.host.stop()


async def test_a_products_ingestor_replaces_the_built_in_one_without_any_setting(tmp_path: Path) -> None:
    class Shelf(BaseIngestor):
        stored: ClassVar[list[str]] = []

        async def ingest(self, item: Any) -> SubjectId:
            self.stored.append(str(item.body))
            return SubjectId(f"shelf-{len(self.stored)}")

    c = await make(tmp_path, {"oaken_core.ingestors": [EP("items", ItemsIngestor), EP("shelf", Shelf)]})
    try:
        assert c.host._active_name("ingestor") == "shelf"  # the fallback steps aside
        await c.client.call("intake_submit", {"source": "crm", "body": "a contact"}, CLI, confirm=True)
        await c.runner.drain()
        assert Shelf.stored == ["a contact"]  # the product's ingestor stored it
        assert "items.list" not in c.registry  # and the built-in one does not run at all, so offers nothing
    finally:
        await c.host.stop()


async def test_intake_submit_is_off_over_mcp_until_the_owner_turns_it_on(core: Core) -> None:
    mcp = {o.name for o in await core.client.visible(CallerContext.mcp("me"))}
    assert "intake_submit" not in mcp
