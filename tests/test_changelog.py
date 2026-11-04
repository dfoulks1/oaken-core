# SPDX-License-Identifier: Apache-2.0
import pytest

from oaken_core.actors import SYSTEM, USER
from oaken_core.changelog import ChangeLog
from oaken_core.clock import FakeClock
from oaken_core.db import Database
from oaken_core.errors import StorageError


async def _append(db: Database, log: ChangeLog, **kw: object) -> str:
    async with db.transaction() as tx:
        return await log.append(tx, actor=kw.pop("actor", SYSTEM), subject_id="s1", key="k", op="set", **kw)  # type: ignore[arg-type]


async def test_rows_come_back_in_order_with_the_actor(core_db: Database, clock: FakeClock) -> None:
    log = ChangeLog(clock)
    await _append(core_db, log, actor=SYSTEM, new="a")
    clock.advance(seconds=1)
    await _append(core_db, log, actor=USER, old="a", new="b")
    rows = await log.rows(core_db, "s1")
    assert [(r.actor_kind, r.old_value, r.new_value) for r in rows] == [
        ("system", None, "a"),
        ("user", "a", "b"),
    ]


async def test_the_log_refuses_update_and_delete(core_db: Database, clock: FakeClock) -> None:
    log = ChangeLog(clock)
    row_id = await _append(core_db, log, new="a")
    with pytest.raises(StorageError, match="append-only"):
        await core_db.execute("UPDATE change_log SET new_value = 'x' WHERE id = ?", (row_id,))
    with pytest.raises(StorageError, match="append-only"):
        await core_db.execute("DELETE FROM change_log WHERE id = ?", (row_id,))
    assert len(await log.rows(core_db)) == 1


async def test_a_rolled_back_change_leaves_no_log_row(core_db: Database, clock: FakeClock) -> None:
    log = ChangeLog(clock)
    with pytest.raises(RuntimeError):
        async with core_db.transaction() as tx:
            await log.append(tx, actor=SYSTEM, subject_id="s1", key="k", op="set")
            raise RuntimeError("boom")
    assert await log.rows(core_db) == []
