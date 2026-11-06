# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

from oaken_core.store import SqliteStore, open_core


async def test_open_core_migrates_and_is_idempotent(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "c.db", read_pool_size=1)
    db = await open_core(store)
    await db.close()
    db = await open_core(store)  # second open applies nothing new
    try:
        rows = await db.fetchall("SELECT version, name FROM schema_version")
        assert [(r["version"], r["name"]) for r in rows] == [
            (1, "0001_core"), (2, "0002_jobs"), (3, "0003_agents"), (4, "0004_exposure"), (5, "0005_router"), (6, "0006_budget"), (7, "0007_log")
        ]
        tables = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master")}
        assert {"change_log", "labels"} <= tables
    finally:
        await db.close()
