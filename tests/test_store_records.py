# SPDX-License-Identifier: Apache-2.0
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from oaken_core.errors import Conflict, NotFound
from oaken_core.plugins.contracts import Query
from oaken_core.store import SqliteStore, open_core


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(tmp_path / "c.db")
    db = await open_core(s)
    yield s
    await db.close()


async def test_create_get_update_delete(store: SqliteStore) -> None:
    s = store
    r = await s.create("things", {"a": 1, "b": {"c": 2}}, id="x")
    assert (await s.get("things", "x")) == r and r.rev == 1
    with pytest.raises(Conflict):
        await s.create("things", {}, id="x")
    u = await s.update("things", "x", {"a": 2, "b": None}, rev=1)
    assert u.data == {"a": 2} and u.rev == 2
    with pytest.raises(Conflict):
        await s.update("things", "x", {"a": 3}, rev=1)
    await s.delete("things", "x")
    with pytest.raises(NotFound):
        await s.get("things", "x")
    with pytest.raises(NotFound):
        await s.delete("things", "x")
    with pytest.raises(NotFound):
        await s.update("things", "x", {})


async def test_list_and_count_by_where_has_order_and_page(store: SqliteStore) -> None:
    s = store
    for i in range(5):
        await s.create("n", {"k": i % 2, "p": {"deep": 1} if i == 4 else {}}, id=f"i{i}")
    assert await s.count("n") == 5 and await s.count("other") == 0
    assert {r.id for r in await s.list("n", Query(where={"k": 1}))} == {"i1", "i3"}
    assert [r.id for r in await s.list("n", Query(has=("p.deep",)))] == ["i4"]
    page = Query(newest_first=True, limit=2, offset=1)
    assert len(await s.list("n", page)) == 2 and await s.count("n", page) == 5
    with pytest.raises(Exception):
        await s.list("n", Query(where={"k') OR 1=1 --": 1}))
