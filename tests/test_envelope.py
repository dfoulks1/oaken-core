# SPDX-License-Identifier: Apache-2.0
"""Intake recognizes an ``{"oaken": 1, "body": ...}`` envelope; only a sender inside core is believed about parent and hops."""

import json
from pathlib import Path

from oaken_core.clock import FakeClock
from oaken_core.daemon.core import Core
from oaken_core.items import ItemsIngestor
from oaken_core.plugins.host import PluginTables
from oaken_core.settings import Settings
from oaken_core.store import SqliteStore

from .test_host import EP

ENV = {"oaken": 1, "body": {"x": 1}, "format": "json", "key": "k1", "parent": "p-1", "hops": 2, "labels": {"priority": "high"}}


async def payload(tmp_path: Path, body: object, **kw: object) -> dict[str, object]:
    found = {"oaken_core.stores": [EP("sqlite", SqliteStore)], "oaken_core.ingestors": [EP("items", ItemsIngestor)]}
    db = tmp_path / str(len(list(tmp_path.iterdir())))  # one database per call
    db.mkdir()
    tables = {"store": {"sqlite": {"path": str(db / "core.db")}}}
    core = Core(Settings(plugins=PluginTables(tables=tables)), clock=FakeClock(), discover=lambda g: found.get(g, []))
    await core.open()
    await core.host.start()
    try:
        await core.intake.submit("s", body, **kw)  # type: ignore[arg-type]
        (job,) = await core.runner.jobs.query(kind="intake.format")
        return json.loads(job.payload_json)  # type: ignore[no-any-return]
    finally:
        await core.host.stop()


async def test_an_outside_envelope_gives_up_its_body_format_and_key_only(tmp_path: Path) -> None:
    p = await payload(tmp_path, ENV)
    assert (p["body"], p["format"], p["key"]) == ({"x": 1}, "json", "k1")
    assert p["parent"] is None and p["hops"] == 0 and p["labels"] == {}  # forged provenance and labels are dropped


async def test_an_inside_envelope_carries_parent_hops_and_labels(tmp_path: Path) -> None:
    p = await payload(tmp_path, ENV, trusted=True)
    assert p["parent"] == "p-1" and p["hops"] == 2 and p["labels"] == {"priority": "high"}


async def test_a_bare_body_or_another_version_is_left_alone(tmp_path: Path) -> None:
    assert (await payload(tmp_path, {"x": 1}))["body"] == {"x": 1}
    assert (await payload(tmp_path, {"oaken": 2, "body": 1}))["body"] == {"oaken": 2, "body": 1}


async def _core(tmp_path: Path) -> Core:
    found = {"oaken_core.stores": [EP("sqlite", SqliteStore)], "oaken_core.ingestors": [EP("items", ItemsIngestor)]}
    tables = {"store": {"sqlite": {"path": str(tmp_path / "core.db")}}}
    core = Core(Settings(plugins=PluginTables(tables=tables)), clock=FakeClock(), discover=lambda g: found.get(g, []))
    await core.open()
    return core


async def test_a_trusted_rerun_goes_to_the_hook_and_skips_intake(tmp_path: Path) -> None:
    import pytest

    from oaken_core.errors import InvalidInput

    core = await _core(tmp_path)
    seen: list[tuple[str, str, str, list[str]]] = []

    async def hook(*a: object) -> None:
        seen.append(a)  # type: ignore[arg-type]

    core.intake.rerun_hook = hook
    env = {"oaken": 1, "body": "", "parent": "s-1", "rerun": {"stage": "act", "routes": ["notify"], "source": "gmail:me"}}
    assert str(await core.intake.submit("ui", env, trusted=True)) == "s-1"
    assert seen == [("s-1", "gmail:me", "act", ["notify"], 50)]  # an envelope names no priority: the ingest default
    assert not await core.runner.jobs.query(kind="intake.format")  # no new item was made
    with pytest.raises(InvalidInput):
        await core.intake.submit("ui", {**env, "rerun": {"stage": "triage"}}, trusted=True)
    with pytest.raises(InvalidInput):
        await core.intake.submit("ui", {**env, "rerun": {"stage": "act"}}, trusted=True)  # act needs routes


async def test_an_outside_rerun_is_ignored_and_the_item_is_an_ordinary_arrival(tmp_path: Path) -> None:
    core = await _core(tmp_path)

    async def hook(*a: object) -> None:
        raise AssertionError("an untrusted envelope must not reach the rerun hook")

    core.intake.rerun_hook = hook
    await core.intake.submit("socket", {"oaken": 1, "body": "x", "parent": "s-1", "rerun": {"stage": "enrich"}})
    assert len(await core.runner.jobs.query(kind="intake.format")) == 1
