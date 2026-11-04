# SPDX-License-Identifier: Apache-2.0
"""The store seam. Storage is local and comes from a store plugin (``BaseStore``).

A store hands core a ``Database``: ``execute``, ``fetchone``, ``fetchall``, ``transaction()``.
Core and plugins write portable SQL against it. SQLite is the default and ships in this package as
the first-party plugin ``sqlite`` (entry point group ``oaken_core.stores``). A Postgres store would
be another plugin.

shortcut: the contract is SQL, so a non-SQL store is out; upgrade if one ever appears.
"""

from pathlib import Path
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

from oaken_core import paths
from oaken_core.db import Database, migrate
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
        return await Database.open(path, read_pool_size=pool, busy_timeout_ms=busy)


async def open_core(store: Store) -> Database:
    """Open the store and apply core's migrations. Nothing else opens a database."""
    db = await store.open()
    try:
        await migrate(db, MIGRATIONS)
    except BaseException:
        await db.close()
        raise
    return db
