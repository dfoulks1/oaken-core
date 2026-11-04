# SPDX-License-Identifier: Apache-2.0
import asyncio
import hashlib
from pathlib import Path

import pytest

from oaken_core.db import Database, migrate
from oaken_core.errors import StorageError


async def make_table(db: Database) -> None:
    await db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT NOT NULL)")


async def test_pragmas(db: Database) -> None:
    async def pragma(name: str) -> object:
        row = await db.fetchone(f"PRAGMA {name}")
        assert row is not None
        return row[0]

    assert await pragma("journal_mode") == "wal"
    assert await pragma("foreign_keys") == 1
    assert await pragma("synchronous") == 1  # NORMAL
    assert await pragma("busy_timeout") == 5000
    assert await pragma("query_only") == 1  # reader connections


async def test_execute_fetch_roundtrip(db: Database) -> None:
    await make_table(db)
    assert await db.execute("INSERT INTO t (v) VALUES (?)", ("a",)) == 1
    await db.executemany("INSERT INTO t (v) VALUES (:v)", [{"v": "b"}, {"v": "c"}])
    rows = await db.fetchall("SELECT v FROM t ORDER BY id")
    assert [r["v"] for r in rows] == ["a", "b", "c"]
    one = await db.fetchone("SELECT v FROM t WHERE id = ?", (2,))
    assert one is not None and one["v"] == "b"
    assert await db.fetchone("SELECT v FROM t WHERE id = 99") is None


async def test_readers_cannot_write(db: Database) -> None:
    await make_table(db)
    with pytest.raises(StorageError):
        await db.fetchall("INSERT INTO t (v) VALUES ('x')")


async def test_sql_errors_become_storage_errors(db: Database) -> None:
    with pytest.raises(StorageError):
        await db.fetchall("SELECT * FROM nope")
    with pytest.raises(StorageError):
        await db.execute("INSERT INTO nope VALUES (1)")
    async with db.transaction() as tx:
        with pytest.raises(StorageError):
            await tx.fetchall("SELECT * FROM nope")
        with pytest.raises(StorageError):
            await tx.executemany("INSERT INTO nope VALUES (?)", [(1,)])


async def test_failed_transaction_rolls_back(db: Database) -> None:
    await make_table(db)
    await db.execute("INSERT INTO t (v) VALUES ('kept')")
    with pytest.raises(RuntimeError):
        async with db.transaction() as tx:
            await tx.execute("INSERT INTO t (v) VALUES ('lost')")
            inside = await tx.fetchone("SELECT count(*) AS n FROM t")
            assert inside is not None and inside["n"] == 2  # a tx sees its own writes
            raise RuntimeError("boom")
    rows = await db.fetchall("SELECT v FROM t")
    assert [r["v"] for r in rows] == ["kept"]
    await db.execute("INSERT INTO t (v) VALUES ('after')")  # writer is usable afterwards


async def test_constraint_violation_rolls_back_whole_transaction(db: Database) -> None:
    await make_table(db)
    with pytest.raises(StorageError):
        async with db.transaction() as tx:
            await tx.execute("INSERT INTO t (v) VALUES ('first')")
            await tx.execute("INSERT INTO t (v) VALUES (NULL)")
    assert await db.fetchall("SELECT * FROM t") == []


async def test_cancelled_transaction_rolls_back(db: Database) -> None:
    await make_table(db)
    started = asyncio.Event()

    async def worker() -> None:
        async with db.transaction() as tx:
            await tx.execute("INSERT INTO t (v) VALUES ('cancelled')")
            started.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(worker())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await db.fetchall("SELECT * FROM t") == []
    await db.execute("INSERT INTO t (v) VALUES ('ok')")


async def test_a_cancel_while_begin_runs_leaves_no_transaction_open(db: Database) -> None:
    # A heartbeat cancelled mid-BEGIN once left the writer holding the lock for good.
    await make_table(db)
    task = asyncio.create_task(db.execute("INSERT INTO t (v) VALUES ('cancelled')"))
    await asyncio.sleep(0)  # the task is now awaiting BEGIN IMMEDIATE
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not db._writer.in_transaction
    await db.execute("INSERT INTO t (v) VALUES ('ok')")
    assert [r["v"] for r in await db.fetchall("SELECT v FROM t")] == ["ok"]


async def test_foreign_keys_enforced(db: Database) -> None:
    await db.execute("CREATE TABLE p (id INTEGER PRIMARY KEY)")
    await db.execute("CREATE TABLE c (pid INTEGER REFERENCES p(id))")
    with pytest.raises(StorageError):
        await db.execute("INSERT INTO c VALUES (1)")


async def test_readers_run_during_an_open_write_transaction(db: Database) -> None:
    await make_table(db)
    await db.execute("INSERT INTO t (v) VALUES ('committed')")
    in_tx = asyncio.Event()
    release = asyncio.Event()

    async def writer() -> None:
        async with db.transaction() as tx:
            await tx.execute("INSERT INTO t (v) VALUES ('uncommitted')")
            in_tx.set()
            await release.wait()

    w = asyncio.create_task(writer())
    await in_tx.wait()
    # The write transaction is open and holds the write lock; reads must not wait for it.
    results = await asyncio.wait_for(
        asyncio.gather(*(db.fetchall("SELECT v FROM t") for _ in range(10))), timeout=5
    )
    assert all([r["v"] for r in rows] == ["committed"] for rows in results)  # snapshot, no dirty
    release.set()
    await w
    assert len(await db.fetchall("SELECT v FROM t")) == 2


async def test_concurrent_writers_are_serialised_without_busy_errors(db: Database) -> None:
    await make_table(db)
    await asyncio.gather(
        *(db.execute("INSERT INTO t (v) VALUES (?)", (str(i),)) for i in range(50))
    )
    row = await db.fetchone("SELECT count(*) AS n FROM t")
    assert row is not None and row["n"] == 50


async def test_second_connection_object_sees_data_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "again.db"
    async with await Database.open(path) as a:
        await make_table(a)
        await a.execute("INSERT INTO t (v) VALUES ('x')")
    async with await Database.open(path) as b:
        assert len(await b.fetchall("SELECT * FROM t")) == 1


async def test_attach_readonly(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "raw.db") as raw:
        await make_table(raw)
        await raw.execute("INSERT INTO t (v) VALUES ('raw-row')")
        async with await Database.open(tmp_path / "index.db") as index:
            await index.attach_readonly(tmp_path / "raw.db", "raw")
            rows = await index.fetchall("SELECT v FROM raw.t")
            assert [r["v"] for r in rows] == ["raw-row"]
            with pytest.raises(StorageError):
                await index.execute("INSERT INTO raw.t (v) VALUES ('nope')")
            with pytest.raises(StorageError, match="alias"):
                await index.attach_readonly(tmp_path / "raw.db", "bad alias;")
            with pytest.raises(StorageError, match="cannot attach"):
                await index.attach_readonly(tmp_path / "missing.db", "gone")


async def test_load_extension_fails_gracefully(db: Database, tmp_path: Path) -> None:
    assert await db.load_extension(str(tmp_path / "no-such-extension")) is False
    await make_table(db)  # connections still usable afterwards


# -- migrations -----------------------------------------------------------------------------
def write_migrations(directory: Path, **files: str) -> Path:
    directory.mkdir(exist_ok=True)
    for name, sql in files.items():
        (directory / f"{name}.sql").write_text(sql, encoding="utf-8")
    return directory


async def test_migrations_apply_in_order_and_are_idempotent(db: Database, tmp_path: Path) -> None:
    d = write_migrations(
        tmp_path / "m",
        **{
            "0002_add_col": "ALTER TABLE a ADD COLUMN b TEXT;",
            "0001_create": "CREATE TABLE a (id INTEGER PRIMARY KEY);",
        },
    )
    assert await migrate(db, d) == [1, 2]
    assert await migrate(db, d) == []
    rows = await db.fetchall("SELECT version, name, checksum FROM schema_version ORDER BY version")
    assert [(r["version"], r["name"]) for r in rows] == [(1, "0001_create"), (2, "0002_add_col")]
    sql = (d / "0001_create.sql").read_bytes()
    assert rows[0]["checksum"] == hashlib.sha256(sql).hexdigest()
    await db.execute("INSERT INTO a (b) VALUES ('works')")


async def test_new_migration_applies_on_top(db: Database, tmp_path: Path) -> None:
    d = write_migrations(tmp_path / "m", **{"0001_a": "CREATE TABLE a (x);"})
    assert await migrate(db, d) == [1]
    write_migrations(d, **{"0002_b": "CREATE TABLE b (y);"})
    assert await migrate(db, d) == [2]


async def test_tampered_applied_migration_is_detected(db: Database, tmp_path: Path) -> None:
    d = write_migrations(tmp_path / "m", **{"0001_a": "CREATE TABLE a (x);"})
    await migrate(db, d)
    write_migrations(d, **{"0001_a": "CREATE TABLE a (x, sneaky);"})
    with pytest.raises(StorageError, match="modified or is missing"):
        await migrate(db, d)


async def test_newer_database_is_refused(db: Database, tmp_path: Path) -> None:
    new = write_migrations(
        tmp_path / "new", **{"0001_a": "CREATE TABLE a (x);", "0002_b": "CREATE TABLE b (y);"}
    )
    await migrate(db, new)
    old = write_migrations(tmp_path / "old", **{"0001_a": "CREATE TABLE a (x);"})
    with pytest.raises(StorageError, match="schema version 2"):
        await migrate(db, old)


async def test_failed_migration_rolls_back_and_can_be_retried(db: Database, tmp_path: Path) -> None:
    d = write_migrations(
        tmp_path / "m",
        **{"0001_a": "CREATE TABLE a (x);", "0002_bad": "CREATE TABLE b (y); NOT SQL;"},
    )
    with pytest.raises(StorageError, match="0002_bad"):
        await migrate(db, d)
    assert (
        await db.fetchall("SELECT name FROM sqlite_master WHERE name IN ('a','b','schema_version')")
        == []
    )
    write_migrations(d, **{"0002_bad": "CREATE TABLE b (y);"})
    assert await migrate(db, d) == [1, 2]


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"1_short": "x"}, "NNNN_name"),
        ({"0001_a": "x", "0003_c": "x"}, "contiguous"),
    ],
)
async def test_bad_migration_directories(
    db: Database, tmp_path: Path, files: dict[str, str], message: str
) -> None:
    d = write_migrations(tmp_path / "m", **files)
    with pytest.raises(StorageError, match=message):
        await migrate(db, d)


async def test_empty_migration_directory_is_fine(db: Database, tmp_path: Path) -> None:
    d = tmp_path / "empty"
    d.mkdir()
    assert await migrate(db, d) == []
