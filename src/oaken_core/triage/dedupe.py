# SPDX-License-Identifier: Apache-2.0
"""``dedupe``: reject an item this source has already sent.

The key is, in order: the ``key`` a provider gave ``submit`` (a message id), the value at ``field`` in the body
(``[plugins.triager.dedupe] field = "message.id"``), else a hash of the whole body. Keys are kept per source, so
two sources may reuse an id. Runs first (rank 1), before rules and models: a repeat costs nothing.

Only keys and hashes are stored, never the item. A job that is retried (a later triager failed) is the same
subject and passes; it is not a repeat of itself.

shortcut: the table only grows (a key row is small), and a repeat is judged at triage, so an item that is later
rejected or fails to ingest is still "seen"; ``oaken dedupe forget`` lets one back in. Add an expiry when a source
sends millions.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, ClassVar

from oaken_core.errors import InvalidInput
from oaken_core.intake import IntakeItem, Verdict
from oaken_core.plugins.base import BaseTriager, command
from oaken_core.plugins.contracts import PluginChange, PluginOpContext, PluginPreview
from pydantic import BaseModel, Field


class DedupeConfig(BaseModel):
    field: str | None = Field(None, description="Dotted path in a JSON body that holds the item's own id, used when the provider gave no key.")


class ForgetIn(BaseModel):
    source: str = Field(description="The source whose keys to forget.")
    key: str | None = Field(None, description="Forget only this key (default: every key of the source).")


class ForgetOut(BaseModel):
    forgotten: int


def _at(body: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(body, dict) or part not in body:
            return None
        body = body[part]
    return body


def key_of(item: IntakeItem, field: str | None) -> str:
    if item.key:
        return item.key
    if field is not None:
        found = _at(item.body, field)
        if found not in (None, ""):
            return f"field:{found}"
    text = item.body if isinstance(item.body, str) else json.dumps(item.body, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


class DedupeTriager(BaseTriager):
    icon = "bi-files"  # duplicates
    api_version = 1  # the plugin API this was written against (core's PLUGIN_API_VERSION)
    requires_core = ">=0.0.0"  # the oaken-core versions it works with
    version: ClassVar[str] = "1"
    rank: ClassVar[int] = 1
    migrations_dir: ClassVar[Path] = Path(__file__).parent / "migrations"
    config_model = DedupeConfig
    config: DedupeConfig

    async def triage(self, item: IntakeItem) -> Verdict | None:
        db = self.ctx.db
        if db is None:
            return None  # a bare context has no table: nothing to compare
        key = key_of(item, self.config.field)
        added = await db.execute(
            "INSERT OR IGNORE INTO dedupe_seen (source, key, subject_id, first_seen) VALUES (?, ?, ?, ?)",
            (item.source, key, item.subject_id, self.ctx.clock.now().isoformat()),
        )
        if added:
            return None
        row = await db.fetchone("SELECT subject_id, first_seen FROM dedupe_seen WHERE source = ? AND key = ?", (item.source, key))
        if row is not None and row["subject_id"] == item.subject_id:
            return None  # a retry of the same job
        return Verdict("reject", f"seen before ({row['first_seen'] if row else 'earlier'})")

    async def _preview_forget(self, ctx: PluginOpContext, inp: ForgetIn) -> PluginPreview:
        n = await self._count(inp)
        what = f"key {inp.key!r}" if inp.key else "every key"
        return PluginPreview(f"Forget {what} of source {inp.source!r}: {n} item(s) could come in again", (PluginChange(inp.source, "forget", after=n),), n)

    async def _count(self, inp: ForgetIn) -> int:
        db = self.ctx.db
        if db is None:
            raise InvalidInput("no database in this context")
        sql, params = "SELECT COUNT(*) AS n FROM dedupe_seen WHERE source = ?", [inp.source]
        if inp.key:
            sql, params = sql + " AND key = ?", [*params, inp.key]
        row = await db.fetchone(sql, tuple(params))
        return int(row["n"]) if row else 0

    @command("forget", "Let items of a source (or one key) come in again.", input=ForgetIn, output=ForgetOut, permission="mutate", preview="_preview_forget")
    async def forget(self, ctx: PluginOpContext, inp: ForgetIn) -> ForgetOut:
        db = self.ctx.db
        if db is None:
            raise InvalidInput("no database in this context")
        sql, params = "DELETE FROM dedupe_seen WHERE source = ?", [inp.source]
        if inp.key:
            sql, params = sql + " AND key = ?", [*params, inp.key]
        return ForgetOut(forgotten=await db.execute(sql, tuple(params)))
