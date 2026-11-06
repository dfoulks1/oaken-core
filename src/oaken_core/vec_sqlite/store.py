# SPDX-License-Identifier: Apache-2.0
"""A vector store in its own SQLite file, using the sqlite-vec extension (brute-force KNN).

Layout, one file:

* ``vec_collections``  one row per collection: its dimensions, and the name of its vector table.
* ``vec_items``        one row per (collection, key): the row id used in the collection's table.
* ``vec_N``            one ``vec0`` table per collection (float32, cosine). A collection never shares a
  table, so two collections never mix, and a collection never mixes dimensions.

The first vector filed under a collection fixes its dimensions. A later vector of another size is
refused before anything is written, so a refused call leaves the store as it was. ``score`` is
``1 - cosine distance``: 1.0 is the same direction.

Lifted from oaken-mail ``oaken_vec_sqlite`` (cf3b187): the extension loading, the vec0 tables, the
vector checks and the cosine ordering. Mail's models, accounts, filters and chunks are not here.
"""

import asyncio
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar, Final

from oaken_core import paths
from oaken_core.db import Database, Tx
from oaken_core.errors import PluginError, StorageError
from oaken_core.plugins.base import BaseVectorStore, VectorStats
from oaken_core.plugins.contracts import Health, Hit, PluginContext, VectorItem
from oaken_core.vec_sqlite.config import SqliteVecConfig

MAX_K: Final = 4096  # sqlite-vec refuses a larger k
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS vec_collections (collection TEXT PRIMARY KEY, dimensions INTEGER NOT NULL, tbl TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS vec_items (id INTEGER PRIMARY KEY AUTOINCREMENT, collection TEXT NOT NULL, key TEXT NOT NULL, UNIQUE (collection, key))",
)


def _check_vector(vector: Sequence[float]) -> None:
    if not vector:
        raise PluginError("sqlite-vec: a vector cannot be empty")
    if not all(math.isfinite(x) for x in vector):
        raise PluginError("sqlite-vec: a vector contains NaN or infinity")
    if not any(vector):
        raise PluginError("sqlite-vec: a zero vector has no direction (cosine is undefined)")


class SqliteVecStore(BaseVectorStore):
    """The default vector store: embeddings kept in a local SQLite file (the sqlite-vec extension), searched by distance. ``path`` defaults to
    ``vectors.db`` in the data folder (or the instance's ``path`` setting)."""

    version: ClassVar[str] = "1"
    config_model = SqliteVecConfig

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._db: Database | None = None
        self._lock = asyncio.Lock()  # one writer at a time inside this plugin
        self._sqlite_vec: Any = None

    # -- lifecycle --------------------------------------------------------------------------
    async def start(self, ctx: PluginContext) -> None:
        await super().start(ctx)
        cfg: SqliteVecConfig = self.config
        path = self._path or (Path(cfg.path).expanduser() if cfg.path else paths.data_dir() / "vectors.db")
        try:
            import sqlite_vec  # noqa: PLC0415 - imported here so a missing backend is a plugin error
        except ImportError as exc:
            raise PluginError("sqlite-vec is missing; reinstall oaken-core (`pip install oaken-core`)") from exc
        db = await Database.open(Path(path), read_pool_size=cfg.read_pool_size)
        try:
            if not await db.load_extension(sqlite_vec.loadable_path()):
                raise PluginError(
                    "this Python's SQLite cannot load extensions, so sqlite-vec cannot run; "
                    "use a Python build that allows it (the uv-managed ones do)"
                )
            async with db.transaction() as tx:
                for statement in _SCHEMA:
                    await tx.execute(statement)
        except BaseException:
            await db.close()
            raise
        self._db, self._sqlite_vec = db, sqlite_vec

    async def stop(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def health(self) -> Health:
        try:
            row = await self._open().fetchone("SELECT vec_version() AS v")
        except (StorageError, PluginError) as exc:
            return Health(False, str(exc))
        return Health(True, f"sqlite-vec {row['v'] if row else '?'}")

    def _open(self) -> Database:
        if self._db is None:
            raise PluginError("sqlite-vec: the store is not started")
        return self._db

    def _blob(self, vector: Sequence[float]) -> bytes:
        return bytes(self._sqlite_vec.serialize_float32(list(vector)))

    # -- writing ----------------------------------------------------------------------------
    @staticmethod
    async def _collection(tx: Tx | Database, name: str) -> dict[str, Any] | None:
        row = await tx.fetchone("SELECT collection, dimensions, tbl FROM vec_collections WHERE collection = ?", (name,))
        return dict(row) if row else None

    async def upsert(self, collection: str, items: Sequence[VectorItem]) -> None:
        """File each item under its key, replacing a vector already there. All of it or none of it: a
        vector the collection cannot hold (another size, NaN, a zero) refuses the whole call."""
        if not items:
            return
        if not collection:
            raise PluginError("sqlite-vec: a collection needs a name")
        for item in items:
            _check_vector(item.vector)
        sizes = {len(item.vector) for item in items}
        if len(sizes) > 1:
            raise PluginError(f"sqlite-vec: one call mixes vector sizes {sorted(sizes)}")
        (size,) = sizes
        async with self._lock, self._open().transaction() as tx:
            found = await self._collection(tx, collection)
            if found is None:
                row = await tx.fetchone("SELECT COUNT(*) AS n FROM vec_collections")
                table = f"vec_{(row['n'] if row else 0) + 1}"
                while await tx.fetchone("SELECT 1 FROM vec_collections WHERE tbl = ?", (table,)):
                    table += "x"
                await tx.execute(
                    f"CREATE VIRTUAL TABLE {table} USING vec0(embedding float[{int(size)}] distance_metric=cosine)"  # noqa: S608 - vec_<int>
                )
                await tx.execute("INSERT INTO vec_collections (collection, dimensions, tbl) VALUES (?, ?, ?)", (collection, size, table))
                found = {"collection": collection, "dimensions": size, "tbl": table}
            elif found["dimensions"] != size:
                raise PluginError(f"sqlite-vec: {collection!r} holds {found['dimensions']}-dimensional vectors, not {size}")
            for item in items:
                row = await tx.fetchone("SELECT id FROM vec_items WHERE collection = ? AND key = ?", (collection, item.key))
                if row is not None:
                    await tx.execute(f"DELETE FROM {found['tbl']} WHERE rowid = ?", (row["id"],))  # noqa: S608
                    rowid = row["id"]
                else:
                    await tx.execute("INSERT INTO vec_items (collection, key) VALUES (?, ?)", (collection, item.key))
                    rowid = (await tx.fetchone("SELECT last_insert_rowid() AS id"))["id"]  # type: ignore[index]
                await tx.execute(f"INSERT INTO {found['tbl']} (rowid, embedding) VALUES (?, ?)", (rowid, self._blob(item.vector)))  # noqa: S608

    async def delete(self, collection: str, keys: Sequence[str]) -> None:
        """Remove these keys from the collection. A key that is not there is not an error."""
        async with self._lock, self._open().transaction() as tx:
            found = await self._collection(tx, collection)
            if found is None:
                return
            for key in keys:
                row = await tx.fetchone("SELECT id FROM vec_items WHERE collection = ? AND key = ?", (collection, key))
                if row is not None:
                    await tx.execute(f"DELETE FROM {found['tbl']} WHERE rowid = ?", (row["id"],))  # noqa: S608
                    await tx.execute("DELETE FROM vec_items WHERE id = ?", (row["id"],))

    # -- reading ----------------------------------------------------------------------------
    async def query(self, collection: str, vector: Sequence[float], k: int) -> list[Hit]:
        """The ``k`` nearest keys in the collection, best first, with score ``1 - cosine distance``.
        An unknown collection is empty, not an error."""
        _check_vector(vector)
        if k < 1:
            raise PluginError("sqlite-vec: k must be at least 1")
        db = self._open()
        found = await self._collection(db, collection)
        if found is None:
            return []
        if found["dimensions"] != len(vector):
            raise PluginError(f"sqlite-vec: {collection!r} holds {found['dimensions']}-dimensional vectors, not {len(vector)}")
        rows = await db.fetchall(
            f"SELECT i.key, m.distance FROM "  # noqa: S608 - vec_<int>
            f"(SELECT rowid, distance FROM {found['tbl']} WHERE embedding MATCH ? AND k = ?) m "
            "JOIN vec_items i ON i.id = m.rowid ORDER BY m.distance, i.key",
            (self._blob(vector), min(k, MAX_K)),
        )
        return [Hit(r["key"], 1.0 - float(r["distance"])) for r in rows]

    async def stats(self, collection: str | None = None) -> VectorStats:
        """How many vectors are held: in one collection, or in all of them."""
        db = self._open()
        if collection is not None:
            found = await self._collection(db, collection)
            if found is None:
                return VectorStats(0, 0, {})
            row = await db.fetchone("SELECT COUNT(*) AS n FROM vec_items WHERE collection = ?", (collection,))
            n = row["n"] if row else 0
            return VectorStats(n, found["dimensions"], {collection: n})
        rows = await db.fetchall("SELECT collection, COUNT(*) AS n FROM vec_items GROUP BY collection ORDER BY collection")
        by = {r["collection"]: r["n"] for r in rows}
        return VectorStats(sum(by.values()), 0, by)
