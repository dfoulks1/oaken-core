# SPDX-License-Identifier: Apache-2.0
"""The router's answer cache is the ``cache_store`` kind: core's table by default, a plugin on request."""

from typing import Any

import pytest

from oaken_core.clock import FakeClock
from oaken_core.db import Database
from oaken_core.plugins.base import BaseCache
from oaken_core.plugins.contracts import CacheEntry, CacheStats
from oaken_core.router.router import Router
from oaken_core.router.store import AnswerCache
from oaken_core.settings import RouterSettings
from oaken_core.testing.contracts import CacheContract, verify_cache

from .test_router import Dir


class MemoryCache(BaseCache):
    """A cache plugin with no database: what an author might write to keep answers somewhere else."""

    version = "1"

    def __init__(self) -> None:
        self.items: dict[tuple[str, ...], CacheEntry] = {}

    def key(self, analyzer: str, version: str, content_hash: str, model: str) -> tuple[str, ...]:
        return (analyzer, version, content_hash, model)

    async def get(self, key: tuple[str, ...]) -> CacheEntry | None:
        return self.items.get(key)

    async def put(self, key: tuple[str, ...], entry: CacheEntry) -> None:
        self.items[key] = entry

    async def stats(self) -> CacheStats:
        by: dict[str, int] = {}
        for k in self.items:
            by[k[0]] = by.get(k[0], 0) + 1
        return CacheStats(entries=len(self.items), bytes=sum(len(e.result_json) for e in self.items.values()), by_analyzer=dict(sorted(by.items())))

    async def clear(self, analyzer: str | None = None) -> int:
        gone = [k for k in self.items if analyzer is None or k[0] == analyzer]
        for k in gone:
            del self.items[k]
        return len(gone)


class TestMemoryCache(CacheContract):
    @pytest.fixture
    def cache(self) -> MemoryCache:
        return MemoryCache()


async def test_the_suite_passes_as_one_call() -> None:
    await verify_cache(MemoryCache)


class TestCoresOwnCache(CacheContract):
    """Core's default meets the same contract a plugin does."""

    @pytest.fixture
    async def cache(self, core_db: Database) -> AnswerCache:
        return AnswerCache(core_db, FakeClock())

    async def test_is_valid(self, cache: Any, cache_config: Any) -> None:
        pass  # a table, not a plugin: there is no plugin shape to validate


def router(db: Database, clock: FakeClock, **settings: Any) -> tuple[Router, Dir]:
    d = Dir()
    return Router(db, clock, RouterSettings(**settings), d), d


async def test_the_router_uses_core_s_table_unless_a_healthy_cache_plugin_is_named(core_db: Database, clock: FakeClock) -> None:
    r, d = router(core_db, clock)
    assert r.cache is r.builtin_cache
    mem = MemoryCache()
    d.cache = lambda name: mem if name == "mem" else None  # type: ignore[attr-defined]
    r2, _ = router(core_db, clock, cache_store="mem")
    r2.directory = d
    assert r2.cache is mem
    r3, _ = router(core_db, clock, cache_store="down")  # named but not healthy: fall back, never fail
    r3.directory = d
    assert r3.cache is r3.builtin_cache
