# SPDX-License-Identifier: Apache-2.0
"""A stored item is enriched, then acted on for each route its triager asked for."""

from pathlib import Path
from typing import Any, ClassVar

from oaken_core.clock import FakeClock
from oaken_core.daemon.core import Core
from oaken_core.intake import IntakeItem, Verdict
from oaken_core.items import ItemsIngestor
from oaken_core.operations.caller import CallerContext
from oaken_core.plugins.base import BaseAction, BaseEnricher, BaseTriager
from oaken_core.plugins.host import PluginTables
from oaken_core.settings import Settings
from oaken_core.stages import ActionResult, ActionTarget, EnrichTarget, Enrichment
from oaken_core.store import SqliteStore

from .test_host import EP

CLI = CallerContext.cli()
SEEN: list[Any] = []


class Router(BaseTriager):
    async def triage(self, item: IntakeItem) -> Verdict | None:
        return Verdict("accept", route=("save", "ghost"))


class Count(BaseEnricher):
    async def enrich(self, target: EnrichTarget) -> Enrichment | None:
        return Enrichment({"chars": len(str(target.body))})


class Twice(BaseEnricher):  # sees what Count added
    async def enrich(self, target: EnrichTarget) -> Enrichment | None:
        return Enrichment({"seen": sorted(target.enrichments)})


class Save(BaseAction):
    routes: ClassVar[tuple[str, ...]] = ("save",)

    async def act(self, target: ActionTarget) -> ActionResult:
        SEEN.append((target.route, target.enrichments["Count"].data))
        return ActionResult(True, "saved")


async def make(tmp_path: Path) -> Core:
    found = {"oaken_core.stores": [EP("sqlite", SqliteStore)], "oaken_core.ingestors": [EP("items", ItemsIngestor)],
             "oaken_core.triagers": [EP("router", Router)], "oaken_core.enrichers": [EP("Count", Count), EP("Twice", Twice)],
             "oaken_core.actions": [EP("save", Save)]}  # fmt: skip
    tables = {"store": {"sqlite": {"path": str(tmp_path / "core.db")}}}
    core = Core(Settings(plugins=PluginTables(tables=tables)), clock=FakeClock(), discover=lambda g: found.get(g, []))
    await core.open()
    await core.host.start()
    q = core.runner.jobs.queues
    q.new_queue("enrich", kind="enricher")
    q.new_queue("act", kind="action")
    return core


async def test_enrichers_run_in_a_stage_and_actions_run_per_route(tmp_path: Path) -> None:
    SEEN.clear()
    core = await make(tmp_path)
    try:
        await core.client.call("intake_submit", {"source": "s", "body": "hello"}, CLI, confirm=True)
        await core.runner.drain()
        (item,) = (await core.client.call("items.list", {}, CLI)).output.items  # type: ignore[union-attr]
        assert SEEN == [("save", {"chars": 5})]  # the action saw the enrichment
        kept = await core.host.store.get("enrichments", f"{item.subject_id}:Twice")
        assert kept.data["data"] == {"seen": ["Count"]}  # kept in the store, and Twice saw Count's
        failed = [j for j in await core.runner.jobs.query(status="failed")]
        assert [j.last_error for j in failed] == ["PermanentJobError: no action answers route 'ghost'"]  # parked, not lost
    finally:
        await core.host.stop()
