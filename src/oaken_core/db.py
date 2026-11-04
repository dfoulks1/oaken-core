# SPDX-License-Identifier: Apache-2.0
"""Async-safe SQLite access (see docs/adr/0003-sqlite-access-layer.md).

One *write broker* and a *pool of read connections*, all in WAL mode:

* Writes go through a single writer connection. SQLite allows one writer at a time, so the
  broker serialises them in arrival order; waiting costs an ``asyncio`` lock, never a blocked
  event loop and never an ``SQLITE_BUSY``.
* Reads are served by separate read-only connections, each with its own thread (via
  ``aiosqlite``). Under WAL they run concurrently with the writer and with each other, so a slow
  write or a slow query never queues the others behind it.

Everything inside ``transaction()`` (including reads that must see its own writes) uses the
writer. Plain ``fetchone``/``fetchall`` see the last committed state.
"""

import asyncio
import hashlib
import os
import re
import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import aiosqlite

from oaken_core.errors import StorageError

Params = Sequence[Any] | dict[str, Any]
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MIGRATION = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class Tx:
    """The writer connection, lent out for the duration of one ``transaction()``."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: Params = ()) -> int:
        """Run one statement; returns the number of rows changed."""
        try:
            cur = await self._conn.execute(sql, params)
            try:
                return cur.rowcount
            finally:
                await cur.close()
        except sqlite3.Error as exc:
            raise StorageError(str(exc)) from exc

    async def executemany(self, sql: str, rows: Sequence[Params]) -> None:
        try:
            await self._conn.executemany(sql, rows)
        except sqlite3.Error as exc:
            raise StorageError(str(exc)) from exc

    async def fetchone(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        rows = await self.fetchall(sql, params)
        return rows[0] if rows else None

    async def fetchall(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        try:
            async with self._conn.execute(sql, params) as cur:
                return list(await cur.fetchall())
        except sqlite3.Error as exc:
            raise StorageError(str(exc)) from exc


class Database:
    """One SQLite file: a write broker plus a pool of read-only connections."""

    def __init__(
        self,
        path: Path,
        writer: aiosqlite.Connection,
        readers: list[aiosqlite.Connection],
    ) -> None:
        self.path = path
        self._writer = writer
        self._readers = readers
        self._write_lock = asyncio.Lock()
        self._pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
        for r in readers:
            self._pool.put_nowait(r)
        self._attached: list[tuple[Path, str]] = []

    # -- lifecycle --------------------------------------------------------------------------
    @classmethod
    async def open(
        cls, path: Path, *, read_pool_size: int = 4, busy_timeout_ms: int = 5000
    ) -> Self:
        path = _prepare(path)
        _create_private(path)
        writer = await _connect(path, busy_timeout_ms, read_only=False)
        try:
            await _pragma(writer, "journal_mode=WAL")  # persists in the file
            readers = [
                await _connect(path, busy_timeout_ms, read_only=True) for _ in range(read_pool_size)
            ]
        except BaseException:
            await writer.close()
            raise
        _private(path)
        return cls(path, writer, readers)

    async def close(self) -> None:
        for conn in [self._writer, *self._readers]:
            with suppress(ValueError, sqlite3.Error):
                await conn.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def _all(self) -> list[aiosqlite.Connection]:
        return [self._writer, *self._readers]

    # -- writes -----------------------------------------------------------------------------
    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Tx]:
        """``BEGIN IMMEDIATE`` ... commit, or roll back if the block raises (or is cancelled)."""
        async with self._write_lock:
            try:
                await _settle(self._writer, "BEGIN IMMEDIATE")
                yield Tx(self._writer)
                await _settle(self._writer, "COMMIT")
            except BaseException:
                # Also reached when BEGIN ran but its caller was cancelled, or COMMIT failed:
                # either way the writer must not be left holding the file's write lock.
                if self._writer.in_transaction:
                    await _settle(self._writer, "ROLLBACK")
                raise

    async def execute(self, sql: str, params: Params = ()) -> int:
        async with self.transaction() as tx:
            return await tx.execute(sql, params)

    async def executemany(self, sql: str, rows: Sequence[Params]) -> None:
        async with self.transaction() as tx:
            await tx.executemany(sql, rows)

    async def vacuum(self) -> None:
        """Rewrite the file without the space deleted rows left, and shrink its WAL. SQLite cannot
        do this inside a transaction, so it holds the write lock for the duration: reads go on
        (WAL), writes wait. It needs about the file's own size free on the disk while it runs."""
        async with self._write_lock:
            try:
                # Each statement's cursor is read to the end and closed: SQLite refuses to vacuum
                # while any statement on the connection is still "in progress".
                # In WAL mode the rewrite lands in the log, so a second checkpoint is what makes
                # the file itself smaller.
                for statement in (
                    "PRAGMA wal_checkpoint(TRUNCATE)",
                    "VACUUM",
                    "PRAGMA wal_checkpoint(TRUNCATE)",
                ):
                    async with self._writer.execute(statement) as cursor:
                        await cursor.fetchall()
            except sqlite3.Error as exc:
                raise StorageError(f"vacuum failed: {exc}") from exc

    # -- reads ------------------------------------------------------------------------------
    @asynccontextmanager
    async def _reader(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await self._pool.get()
        try:
            yield conn
        finally:
            self._pool.put_nowait(conn)

    async def fetchall(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        async with self._reader() as conn:
            try:
                async with conn.execute(sql, params) as cur:
                    return list(await cur.fetchall())
            except sqlite3.Error as exc:
                raise StorageError(str(exc)) from exc

    async def fetchone(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        rows = await self.fetchall(sql, params)
        return rows[0] if rows else None

    # -- attach and extensions --------------------------------------------------------------
    async def attach_readonly(self, other: Path, alias: str) -> None:
        """Make ``other`` visible as schema ``alias`` on every connection, read-only."""
        if not _IDENT.match(alias):
            raise StorageError(f"invalid schema alias {alias!r}")
        uri = _readonly_uri(other)
        async with self._write_lock:
            for conn in self._all:
                try:
                    await conn.execute(f"ATTACH DATABASE ? AS {alias}", (uri,))
                except sqlite3.Error as exc:
                    raise StorageError(f"cannot attach {other.name}: {exc}") from exc
        self._attached.append((other, alias))

    async def load_extension(self, path: str) -> bool:
        """Load a SQLite extension on every connection. Returns False if this Python/SQLite
        build cannot load extensions or the extension fails to load; never raises for that."""
        async with self._write_lock:
            for conn in self._all:
                try:
                    await conn.enable_load_extension(True)
                    await conn.load_extension(path)
                except (AttributeError, sqlite3.Error, OSError):
                    return False
                finally:
                    with suppress(AttributeError, sqlite3.Error):
                        await conn.enable_load_extension(False)
        return True


def _prepare(path: Path) -> Path:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _create_private(path: Path) -> None:
    """Create the file owner-only before SQLite does; -wal and -shm copy its mode."""
    with suppress(OSError):
        os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))


def _private(path: Path) -> None:
    """Tighten a pre-existing database and its -wal/-shm to owner-only."""
    for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        with suppress(OSError, NotImplementedError):
            p.chmod(0o600)


def _readonly_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


async def _pragma(conn: aiosqlite.Connection, setting: str) -> None:
    """Run a PRAGMA and drain it: an unfinished statement would keep its lock."""
    async with conn.execute(f"PRAGMA {setting}") as cur:
        await cur.fetchall()


async def _settle(conn: aiosqlite.Connection, sql: str) -> None:
    """Run ``sql`` to the end even if the caller is cancelled, then pass the cancel on. aiosqlite
    runs a statement in its thread either way; leaving early loses track of an open transaction."""
    task = asyncio.ensure_future(conn.execute(sql))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def _connect(path: Path, busy_timeout_ms: int, *, read_only: bool) -> aiosqlite.Connection:
    # isolation_level=None: autocommit; transactions are explicit (BEGIN/COMMIT) in this module.
    conn = await aiosqlite.connect(path, isolation_level=None, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        await _pragma(conn, f"busy_timeout={int(busy_timeout_ms)}")
        await _pragma(conn, "foreign_keys=ON")
        await _pragma(conn, "synchronous=NORMAL")
        if read_only:
            await _pragma(conn, "query_only=ON")
    except BaseException:
        await conn.close()
        raise
    return conn


# -- migrations -----------------------------------------------------------------------------
_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
)
"""


def _load_migrations(directory: Path) -> dict[int, tuple[str, str, str]]:
    """version -> (name, sql, sha256). Files must be NNNN_name.sql with no gaps or repeats."""
    found: dict[int, tuple[str, str, str]] = {}
    for f in sorted(directory.glob("*.sql")):
        m = _MIGRATION.match(f.name)
        if not m:
            raise StorageError(f"migration file {f.name!r} must be named NNNN_name.sql")
        version = int(m.group(1))
        if version in found:
            raise StorageError(f"two migrations share version {version:04d}")
        data = f.read_bytes()
        found[version] = (f.stem, data.decode("utf-8"), hashlib.sha256(data).hexdigest())
    if found and sorted(found) != list(range(1, max(found) + 1)):
        raise StorageError("migration versions must be contiguous, starting at 0001")
    return found


def _statements(script: str) -> list[str]:
    """Split a SQL script into complete statements (trigger bodies stay in one piece).
    ``executescript`` is avoided because it commits the surrounding transaction."""
    out: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())  # unterminated tail: let SQLite report the error
    return [s for s in out if s.strip("; \n")]


async def migrate(db: Database, directory: Path) -> list[int]:
    """Apply every migration not yet applied, in order. Forward-only and idempotent.

    Raises StorageError if an applied migration's file changed (tamper), or if the database
    holds a version this code does not know (a newer database). Returns the versions applied.
    """
    available = _load_migrations(directory)
    applied_now: list[int] = []
    async with db.transaction() as tx:
        await tx.execute(_SCHEMA_VERSION)
        rows = await tx.fetchall("SELECT version, name, checksum FROM schema_version")
        applied = {r["version"]: (r["name"], r["checksum"]) for r in rows}
        newest = max(available, default=0)
        if applied and max(applied) > newest:
            raise StorageError(
                f"database is at schema version {max(applied)} but this code only knows {newest}"
                "; refusing to run (upgrade Oaken)"
            )
        for version, (name, checksum) in sorted(applied.items()):
            if version not in available or available[version][2] != checksum:
                raise StorageError(f"applied migration {name!r} was modified or is missing")
        for version in sorted(available):
            if version in applied:
                continue
            name, sql, checksum = available[version]
            try:
                for statement in _statements(sql):
                    await tx.execute(statement)
            except StorageError as exc:
                raise StorageError(f"migration {name} failed: {exc.message}") from exc
            await tx.execute(
                "INSERT INTO schema_version (version, name, checksum) VALUES (?, ?, ?)",
                (version, name, checksum),
            )
            applied_now.append(version)
    return applied_now
