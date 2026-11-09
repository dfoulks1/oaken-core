# SPDX-License-Identifier: Apache-2.0
"""The store seam. Storage is local and comes from a store plugin (``BaseStore``).

A store hands core a ``Database``: ``execute``, ``fetchone``, ``fetchall``, ``transaction()``.
Core and plugins write portable SQL against it. SQLite is the default and ships in this package as
the first-party plugin ``sqlite`` (entry point group ``oaken_core.stores``). A Postgres store would
be another plugin.

shortcut: the contract is SQL, so a non-SQL store is out; upgrade if one ever appears.
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

from oaken_core import paths
from oaken_core.clock import SystemClock, fmt_ts
from oaken_core.db import Database, migrate
from oaken_core.dbgen.sqlite import models as rows
from oaken_core.dbgen.sqlite import records as q
from oaken_core.errors import Conflict, InvalidInput, NotFound, PluginError
from oaken_core.ids import new_subject_id
from oaken_core.plugins.contracts import Query, Record
from oaken_core.plugins.base import BaseStore

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


class Store(Protocol):
    async def open(self) -> Database:
        """Open (or create) the database. The caller closes it."""
        ...


class SqliteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = ""  # empty: ``core.db`` under the data directory
    read_pool_size: int = Field(default=4, ge=1)
    busy_timeout_ms: int = Field(default=5000, ge=0)


class SqliteStore(BaseStore):
    """The default store: one local SQLite file, one writer, a pool of readers."""

    api_version = 1  # the plugin API this was written against (core's PLUGIN_API_VERSION)
    requires_core = ">=0.0.0"  # the oaken-core versions it works with
    version: ClassVar[str] = "1"
    config_model = SqliteConfig

    def __init__(self, path: Path | None = None, **options: int) -> None:
        # Usable directly (``SqliteStore(path)``) and as a plugin (config from ``start``).
        self._direct = path
        self._options = options

    def _settings(self) -> tuple[Path, int, int]:
        cfg = self.config if isinstance(self.config, SqliteConfig) else SqliteConfig()
        path = self._direct or (Path(cfg.path).expanduser() if cfg.path else paths.data_dir() / "core.db")
        return (
            path,
            int(self._options.get("read_pool_size", cfg.read_pool_size)),
            int(self._options.get("busy_timeout_ms", cfg.busy_timeout_ms)),
        )

    async def open(self) -> Database:
        path, pool, busy = self._settings()
        self._db = await Database.open(path, read_pool_size=pool, busy_timeout_ms=busy)
        return self._db

    # -- records (the CRUD + GET contract of ``BaseStore``) -------------------------------------

    def _database(self) -> Database:
        db = getattr(self, "_db", None)
        if db is None:
            raise PluginError("sqlite: the store is not open")
        return db

    def _now(self) -> str:
        ctx = getattr(self, "ctx", None)
        return fmt_ts((ctx.clock if ctx else SystemClock()).now())

    @staticmethod
    def _record(row: sqlite3.Row | rows.Record) -> Record:
        if isinstance(row, rows.Record):  # from the generated queries
            return Record(row.collection, row.id_, json.loads(row.data_json), row.rev, row.created_at, row.updated_at)
        return Record(row["collection"], row["id"], json.loads(row["data_json"]), int(row["rev"]), row["created_at"], row["updated_at"])

    async def create(self, collection: str, data: dict[str, Any], id: str | None = None) -> Record:
        rid, now = id or str(new_subject_id()), self._now()
        text = json.dumps(data, default=str)
        try:
            async with self._database().transaction() as tx:
                await q.create_record(tx.conn, collection=collection, id_=rid, data_json=text, created_at=now, updated_at=now)
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"{collection}/{rid} already exists") from exc
        return Record(collection, rid, json.loads(text), 1, now, now)

    async def get(self, collection: str, id: str) -> Record:
        async with self._database().reading() as conn:
            found = await q.get_record(conn, collection=collection, id_=id)
        if found is None:
            raise NotFound(f"no record {collection}/{id}")
        return self._record(found)

    @staticmethod
    def _key(key: str) -> str:
        if not re.fullmatch(r"\w+", key):
            raise InvalidInput(f"not a usable record key: {key!r}")
        return key

    @staticmethod
    def _path(key: str) -> str:
        """A dotted key (``parts.headers``) as a quoted JSON path, so a segment may hold any character but a quote."""
        if '"' in key or not key:
            raise InvalidInput(f"not a usable record key: {key!r}")
        return "$" + "".join(f'."{seg}"' for seg in key.split("."))

    def _conditions(self, collection: str, q: Query) -> tuple[str, list[Any]]:
        cond, args = ["collection = ?"], [collection]
        for key, value in q.where.items():
            cond.append(f"json_extract(data_json, '$.{self._key(key)}') = ?")
            args.append(value)
        for key in q.has:
            cond.append("json_type(data_json, ?) IS NOT NULL")
            args.append(self._path(key))
        for key, value in q.labels:  # the id is a subject; labels are core's own table
            cond.append("EXISTS (SELECT 1 FROM labels l WHERE l.subject_id = records.id AND l.key = ?" + ("" if value is None else " AND l.value = ?") + ")")
            args += [key] if value is None else [key, value]
        if q.since:
            cond.append("created_at >= ?")
            args.append(q.since)
        if q.until:
            cond.append("created_at < ?")
            args.append(q.until)
        return " WHERE " + " AND ".join(cond), args

    async def list(self, collection: str, query: Query | None = None) -> "list[Record]":
        q = query or Query()
        sql, args = self._conditions(collection, q)
        order = "DESC" if q.newest_first else "ASC"
        by = f"json_extract(data_json, '$.{self._key(q.order_by)}')" if q.order_by else "created_at"
        rows = await self._database().fetchall(
            f"SELECT * FROM records{sql} ORDER BY {by} {order}, id {order} LIMIT ? OFFSET ?", (*args, q.limit, q.offset)
        )
        return [self._record(r) for r in rows]

    async def count(self, collection: str, query: Query | None = None) -> int:
        sql, args = self._conditions(collection, query or Query())
        row = await self._database().fetchone(f"SELECT COUNT(*) AS n FROM records{sql}", tuple(args))
        return int(row["n"]) if row else 0

    async def update(self, collection: str, id: str, data: dict[str, Any], rev: int | None = None) -> Record:
        async with self._database().transaction() as tx:
            found = await q.get_record(tx.conn, collection=collection, id_=id)
            if found is None:
                raise NotFound(f"no record {collection}/{id}")
            if rev is not None and rev != found.rev:
                raise Conflict(f"{collection}/{id} is at rev {found.rev}, not {rev}")
            merged = {**json.loads(found.data_json), **json.loads(json.dumps(data, default=str))}
            merged = {k: v for k, v in merged.items() if v is not None}  # a None in ``data`` removes the key
            now = self._now()
            await q.update_record(tx.conn, data_json=json.dumps(merged), updated_at=now, collection=collection, id_=id)
            return Record(collection, id, merged, found.rev + 1, found.created_at, now)

    async def delete(self, collection: str, id: str) -> None:
        async with self._database().transaction() as tx:
            gone = await q.delete_record(tx.conn, collection=collection, id_=id)
        if not gone:
            raise NotFound(f"no record {collection}/{id}")


async def open_core(store: Store) -> Database:
    """Open the store and apply core's migrations. Nothing else opens a database."""
    db = await store.open()
    try:
        await migrate(db, MIGRATIONS)
    except BaseException:
        await db.close()
        raise
    return db
