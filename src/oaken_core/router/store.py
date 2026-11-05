# SPDX-License-Identifier: Apache-2.0
"""The answer cache and the usage ledger, both in core's database.

The cache design follows oaken-mail ``ai/cache.py`` (cf3b187), re-keyed on (analyzer, version,
content hash, effective model). The ledger is new: oaken-mail wrote one ``ai_usage`` row per call;
here a row is a running counter per (analyzer, model, backend), which is what a report needs.
"""

from dataclasses import dataclass
from datetime import timedelta

from oaken_core.clock import Clock, fmt_ts, parse_ts
from oaken_core.db import Database
from oaken_core.plugins.contracts import CacheEntry, CacheStats

__all__ = ["AnswerCache", "CacheEntry", "CacheStats", "Ledger", "LedgerRow", "Spend", "SpendRow"]


@dataclass(frozen=True)
class LedgerRow:
    analyzer: str
    model: str
    backend: str
    calls: int
    tokens_in: int
    tokens_out: int
    cache_hits: int
    shortcut_hits: int
    escalations: int
    tokens_saved: int


class AnswerCache:
    """The default ``cache_store``: core's own table, which a plugin never sees. A ``cache_store`` plugin
    replaces it with the same five members (``key``, ``get``, ``put``, ``stats``, ``clear``)."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self.db, self.clock = db, clock

    def key(self, analyzer: str, version: str, content_hash: str, model: str) -> tuple[str, ...]:
        return (analyzer, version, content_hash, model)

    async def get(self, key: tuple[str, ...]) -> CacheEntry | None:
        row = await self.db.fetchone(
            "SELECT result_json, tokens_in, tokens_out FROM answer_cache WHERE analyzer = ? AND"
            " version = ? AND content_hash = ? AND model = ?",
            key,
        )
        return None if row is None else CacheEntry(row["result_json"], int(row["tokens_in"]), int(row["tokens_out"]))

    async def put(self, key: tuple[str, ...], entry: CacheEntry) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO answer_cache (analyzer, version, content_hash, model, result_json,"
            " tokens_in, tokens_out, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (*key, entry.result_json, entry.tokens_in, entry.tokens_out, fmt_ts(self.clock.now())),
        )

    async def stats(self) -> CacheStats:
        total = await self.db.fetchone("SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(result_json)), 0) AS b, MIN(created_at) AS oldest FROM answer_cache")
        by = await self.db.fetchall("SELECT analyzer, COUNT(*) AS n FROM answer_cache GROUP BY analyzer ORDER BY analyzer")
        assert total is not None
        return CacheStats(entries=int(total["n"]), bytes=int(total["b"]), by_analyzer={r["analyzer"]: int(r["n"]) for r in by},
                          oldest=parse_ts(total["oldest"]) if total["oldest"] else None)  # fmt: skip

    async def clear(self, analyzer: str | None = None) -> int:
        return await self.db.execute("DELETE FROM answer_cache WHERE (? IS NULL OR analyzer = ?)", (analyzer, analyzer))


class Ledger:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db, self.clock = db, clock

    async def add(
        self, analyzer: str, model: str, backend: str, *, calls: int = 0, tokens_in: int = 0,
        tokens_out: int = 0, cache_hits: int = 0, shortcut_hits: int = 0, escalations: int = 0,
        tokens_saved: int = 0,
    ) -> None:  # fmt: skip
        await self.db.execute(
            "INSERT INTO llm_ledger (analyzer, model, backend, calls, tokens_in, tokens_out, cache_hits,"
            " shortcut_hits, escalations, tokens_saved, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (analyzer, model, backend) DO UPDATE SET calls = calls + excluded.calls,"
            " tokens_in = tokens_in + excluded.tokens_in, tokens_out = tokens_out + excluded.tokens_out,"
            " cache_hits = cache_hits + excluded.cache_hits,"
            " shortcut_hits = shortcut_hits + excluded.shortcut_hits,"
            " escalations = escalations + excluded.escalations,"
            " tokens_saved = tokens_saved + excluded.tokens_saved, updated_at = excluded.updated_at",
            (analyzer, model, backend, calls, tokens_in, tokens_out, cache_hits, shortcut_hits,
             escalations, tokens_saved, fmt_ts(self.clock.now())),
        )  # fmt: skip

    async def rows(self, analyzer: str | None = None) -> list[LedgerRow]:
        found = await self.db.fetchall(
            "SELECT analyzer, model, backend, calls, tokens_in, tokens_out, cache_hits, shortcut_hits,"
            " escalations, tokens_saved FROM llm_ledger WHERE (? IS NULL OR analyzer = ?)"
            " ORDER BY analyzer, model, backend",
            (analyzer, analyzer),
        )
        return [LedgerRow(**{k: r[k] for k in LedgerRow.__dataclass_fields__}) for r in found]


@dataclass(frozen=True)
class SpendRow:
    analyzer: str
    day: str
    tokens: int
    deferrals: int


class Spend:
    """What each analyzer has spent today (UTC), for the optional ``daily_tokens`` budget."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self.db, self.clock = db, clock

    def today(self) -> str:
        return self.clock.now().strftime("%Y-%m-%d")

    async def tokens(self, analyzer: str) -> int:
        row = await self.db.fetchone("SELECT tokens FROM llm_spend WHERE analyzer = ? AND day = ?", (analyzer, self.today()))
        return 0 if row is None else int(row["tokens"])

    async def add(self, analyzer: str, tokens: int = 0, deferrals: int = 0) -> None:
        await self.db.execute(
            "INSERT INTO llm_spend (analyzer, day, tokens, deferrals) VALUES (?,?,?,?)"
            " ON CONFLICT (analyzer, day) DO UPDATE SET tokens = tokens + excluded.tokens,"
            " deferrals = deferrals + excluded.deferrals",
            (analyzer, self.today(), tokens, deferrals),
        )

    def seconds_to_reset(self) -> float:
        now = self.clock.now()
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return (nxt - now).total_seconds()

    async def rows(self, analyzer: str | None = None) -> list[SpendRow]:
        found = await self.db.fetchall(
            "SELECT analyzer, day, tokens, deferrals FROM llm_spend WHERE day = ? AND (? IS NULL OR analyzer = ?)"
            " ORDER BY analyzer",
            (self.today(), analyzer, analyzer),
        )
        return [SpendRow(r["analyzer"], r["day"], int(r["tokens"]), int(r["deferrals"])) for r in found]
