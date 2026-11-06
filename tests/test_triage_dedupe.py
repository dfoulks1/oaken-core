# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest
from oaken_core.clock import FakeClock
from oaken_core.db import Database, migrate
from oaken_core.intake import IntakeItem
from oaken_core.plugins.contracts import PluginContext
from oaken_core.triage.dedupe import DedupeTriager, ForgetIn, key_of


async def make(tmp_path: Path, **config: object):
    db = await Database.open(tmp_path / "t.db")
    await migrate(db, DedupeTriager.migrations_dir, table="schema_version_dedupe")
    t = DedupeTriager()
    await t.start(PluginContext(name="dedupe", kind="triager", clock=FakeClock(), log=None, config=dict(config), db=db))  # type: ignore[arg-type]
    return t, db


def item(body: object = "x", subject: str = "s1", source: str = "gmail", key: str | None = None) -> IntakeItem:
    return IntakeItem(subject, source, body, {}, key)


async def test_the_same_key_twice_is_rejected_and_the_first_passes(tmp_path: Path) -> None:
    t, db = await make(tmp_path)
    assert await t.triage(item(key="m1")) is None
    v = await t.triage(item(subject="s2", key="m1"))
    assert v is not None and v.kind == "reject" and "seen before" in v.reason
    assert await t.triage(item(subject="s3", key="m2")) is None
    await db.close()


async def test_a_retry_of_the_same_subject_passes(tmp_path: Path) -> None:
    t, db = await make(tmp_path)
    assert await t.triage(item(key="m1")) is None
    assert await t.triage(item(key="m1")) is None  # same subject: the job replayed
    await db.close()


async def test_sources_do_not_share_keys(tmp_path: Path) -> None:
    t, db = await make(tmp_path)
    assert await t.triage(item(key="1", source="a")) is None
    assert await t.triage(item(subject="s2", key="1", source="b")) is None
    await db.close()


async def test_with_no_key_the_body_hash_decides(tmp_path: Path) -> None:
    t, db = await make(tmp_path)
    assert await t.triage(item({"a": 1, "b": 2})) is None
    assert (await t.triage(item({"b": 2, "a": 1}, subject="s2"))).kind == "reject"  # type: ignore[union-attr]
    assert await t.triage(item({"a": 1, "b": 3}, subject="s3")) is None
    await db.close()


async def test_a_configured_field_names_the_item(tmp_path: Path) -> None:
    t, db = await make(tmp_path, field="msg.id")
    assert await t.triage(item({"msg": {"id": 7}, "n": 1})) is None
    assert (await t.triage(item({"msg": {"id": 7}, "n": 2}, subject="s2"))).kind == "reject"  # type: ignore[union-attr]
    await db.close()


def test_key_order_is_a_providers_key_then_field_then_hash() -> None:
    assert key_of(item({"id": 1}, key="k"), "id") == "k"
    assert key_of(item({"id": 1}), "id") == "field:1"
    assert key_of(item({"id": 1}), "nope").startswith("sha256:")


async def test_forget_lets_an_item_back_in(tmp_path: Path) -> None:
    t, db = await make(tmp_path)
    await t.triage(item(key="m1"))
    await t.triage(item(subject="s9", key="m2"))
    assert (await t._preview_forget(None, ForgetIn(source="gmail", key="m1"))).summary.startswith("Forget key 'm1'")  # type: ignore[arg-type]
    assert (await t.forget(None, ForgetIn(source="gmail", key="m1"))).forgotten == 1  # type: ignore[arg-type]
    assert await t.triage(item(subject="s2", key="m1")) is None
    assert (await t.forget(None, ForgetIn(source="gmail"))).forgotten == 2  # type: ignore[arg-type]
    await db.close()


async def test_a_bare_context_with_no_database_passes_everything() -> None:
    t = DedupeTriager()
    await t.start(PluginContext(name="dedupe", kind="triager", clock=FakeClock(), log=None, config={}))  # type: ignore[arg-type]
    assert await t.triage(item(key="m1")) is None


# -- through the real host, intake and an ingestor --------------------------------------------------------

from oaken_core.daemon.core import Core  # noqa: E402
from oaken_core.plugins.base import BaseIngestor, BaseProvider  # noqa: E402
from oaken_core.plugins.host import PluginTables  # noqa: E402
from oaken_core.settings import Settings  # noqa: E402
from oaken_core.store import SqliteStore  # noqa: E402
from oaken_core.ids import SubjectId  # noqa: E402

from .test_host import EP  # noqa: E402


class Shelf(BaseIngestor):
    stored: list[IntakeItem] = []

    async def ingest(self, item: IntakeItem) -> SubjectId:
        self.stored.append(item)
        return SubjectId(f"real-{len(self.stored)}")


class Feed(BaseProvider):
    pass


async def test_a_provider_that_resubmits_is_stored_once_by_key_and_by_body(tmp_path: Path) -> None:
    Shelf.stored.clear()
    found = {"oaken_core.stores": [EP("sqlite", SqliteStore)], "oaken_core.triagers": [EP("dedupe", DedupeTriager)],
             "oaken_core.ingestors": [EP("shelf", Shelf)], "oaken_core.providers": [EP("feed", Feed)]}  # fmt: skip
    cfg = {"store": {"sqlite": {"path": str(tmp_path / "core.db")}}}
    core = Core(Settings(plugins=PluginTables(tables=cfg)), clock=FakeClock(), discover=lambda g: found.get(g, []))
    await core.open()
    await core.host.start()
    try:
        feed = core.host.get("provider", "feed")
        await feed.submit({"n": 1}, key="m1")
        await feed.submit({"n": 2}, key="m1")  # same key, different body: a repeat
        await feed.submit({"n": 3}, key="m3")
        await feed.submit({"n": 4})
        await feed.submit({"n": 4})  # no key: the same body is a repeat
        await core.runner.drain()
        got = sorted(i.body["n"] for i in Shelf.stored)  # workers run in parallel: either of the two "m1" items may win
        assert got in ([1, 3, 4], [2, 3, 4])
        assert {i.key for i in Shelf.stored} == {"m1", "m3", None}
    finally:
        await core.host.stop()
