# SPDX-License-Identifier: Apache-2.0
"""The store seam. Storage is local and comes from a store plugin (``BaseStore``).

A store hands core a ``Database``: ``execute``, ``fetchone``, ``fetchall``, ``transaction()``.
Core and plugins write portable SQL against it. SQLite is the default and ships in this package.
A Postgres store would be another implementation of ``Store``.

shortcut: the contract is SQL, so a non-SQL store is out; upgrade if one ever appears.
"""

from pathlib import Path
from typing import Protocol

from oaken_core.db import Database, migrate

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


class Store(Protocol):
    async def open(self) -> Database:
        """Open (or create) the database. The caller closes it."""
        ...


class SqliteStore:
    """The default store: one local SQLite file, one writer, a pool of readers."""

    def __init__(self, path: Path, *, read_pool_size: int = 4, busy_timeout_ms: int = 5000) -> None:
        self.path = path
        self.read_pool_size = read_pool_size
        self.busy_timeout_ms = busy_timeout_ms

    async def open(self) -> Database:
        return await Database.open(
            self.path, read_pool_size=self.read_pool_size, busy_timeout_ms=self.busy_timeout_ms
        )


async def open_core(store: Store) -> Database:
    """Open the store and apply core's migrations. Nothing else opens a database."""
    db = await store.open()
    try:
        await migrate(db, MIGRATIONS)
    except BaseException:
        await db.close()
        raise
    return db
