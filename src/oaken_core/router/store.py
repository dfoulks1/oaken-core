# SPDX-License-Identifier: Apache-2.0
"""The answer cache and the usage ledger, both in core's database.

The cache design follows oaken-mail ``ai/cache.py`` (cf3b187), re-keyed on (analyzer, version,
content hash, effective model). The ledger is new: oaken-mail wrote one ``ai_usage`` row per call;
here a row is a running counter per (analyzer, model, backend), which is what a report needs.
"""

from dataclasses import dataclass

from oaken_core.clock import Clock, fmt_ts
from oaken_core.db import Database


@dataclass(frozen=True)
class CacheEntry:
    result_json: str
    tokens_in: int
    tokens_out: int


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
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db, self.clock = db, clock

    async def get(self, analyzer: str, version: str, content_hash: str, model: str) -> CacheEntry | None:
        row = await self.db.fetchone(
            "SELECT result_json, tokens_in, tokens_out FROM answer_cache WHERE analyzer = ? AND"
            " version = ? AND content_hash = ? AND model = ?",
            (analyzer, version, content_hash, model),
        )
        return None if row is None else CacheEntry(row["result_json"], int(row["tokens_in"]), int(row["tokens_out"]))

    async def put(self, analyzer: str, version: str, content_hash: str, model: str, entry: CacheEntry) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO answer_cache (analyzer, version, content_hash, model, result_json,"
            " tokens_in, tokens_out, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (analyzer, version, content_hash, model, entry.result_json, entry.tokens_in,
             entry.tokens_out, fmt_ts(self.clock.now())),
        )  # fmt: skip

    async def clear(self, analyzer: str | None = None) -> int:
        return await self.db.execute(
            "DELETE FROM answer_cache WHERE (? IS NULL OR analyzer = ?)", (analyzer, analyzer)
        )


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
