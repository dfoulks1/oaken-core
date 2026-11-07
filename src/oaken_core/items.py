# SPDX-License-Identifier: Apache-2.0
"""The built-in ingestor: a table of what was submitted.

Core has no nouns, so something has to say where an accepted item goes. A product supplies that (a CRM's
contacts, a mail client's messages). This one is for plain ``oaken``: it keeps ``(subject, source, body)`` rows so
an item submitted with ``oaken intake submit`` has somewhere to land and can be read back with ``oaken items list``.
It is a fallback: the moment a product installs an ingestor of its own, this one steps aside.
"""

import json
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from oaken_core.clock import fmt_ts
from oaken_core.errors import NotFound, PluginError
from oaken_core.ids import SubjectId, new_subject_id
from oaken_core.plugins.base import BaseIngestor, command
from oaken_core.plugins.contracts import PluginContext, PluginOpContext


class ListIn(BaseModel):
    limit: int = Field(default=20, ge=1, le=200)
    label: list[str] = Field(default_factory=list, description="Only items with this label, as KEY or KEY=VALUE. Repeat for more; all must hold.")
    part: list[str] = Field(default_factory=list, description="Only items that have this part (body, headers...). Repeat for more; all must hold.")
    since: str | None = Field(default=None, description="Only items stored at or after this date or time (2026-11-06, or 2026-11-06T12:00).")
    until: str | None = Field(default=None, description="Only items stored before this date or time.")


class ItemRow(BaseModel):
    subject_id: str
    source: str
    created_at: str
    preview: str  # the first characters of the body, enough to recognise it
    parts: list[str]  # the names of the parts a formatter made of it


class ListOut(BaseModel):
    total: int  # the items that match, not the limit
    items: list[ItemRow]


def where(inp: ListIn) -> tuple[str, list[Any]]:
    """The SQL conditions for a filter (empty when it has none) and their parameters. Values only ever travel as parameters."""
    cond: list[str] = []
    args: list[Any] = []
    for lab in inp.label:
        key, eq, value = lab.partition("=")
        cond.append("EXISTS (SELECT 1 FROM labels l WHERE l.subject_id = items_rows.subject_id AND l.key = ?" + (" AND l.value = ?" if eq else "") + ")")  # either layer: human or machine
        args += [key, value] if eq else [key]
    for name in inp.part:
        cond.append("(EXISTS (SELECT 1 FROM json_each(parts_json) WHERE key = ?) OR (? = 'body' AND parts_json = '{}'))")  # an item stored before parts: its body is its one part
        args += [name, name]
    if inp.since:
        cond.append("created_at >= ?")
        args.append(inp.since)
    if inp.until:
        cond.append("created_at < ?")
        args.append(inp.until)
    return (" WHERE " + " AND ".join(cond)) if cond else "", args


class ItemsIngestor(BaseIngestor):
    api_version = 1  # the plugin API this was written against (core's PLUGIN_API_VERSION)
    requires_core = ">=0.0.0"  # the oaken-core versions it works with
    version: ClassVar[str] = "1"
    fallback: ClassVar[bool] = True
    migrations_dir: ClassVar[Path | None] = Path(__file__).parent / "items_migrations"

    async def start(self, ctx: PluginContext) -> None:
        await super().start(ctx)
        if ctx.db is None:
            raise PluginError("items: no database to store into")

    async def ingest(self, item: Any) -> SubjectId:
        subject = new_subject_id()
        await self.ctx.db.execute(  # type: ignore[union-attr]
            "INSERT INTO items_rows (subject_id, source, body_json, parts_json, created_at) VALUES (?,?,?,?,?)",
            (subject, item.source, json.dumps(item.body, default=str),
             json.dumps({n: p.content for n, p in item.parts.items()}, default=str), fmt_ts(self.ctx.clock.now())),
        )
        return subject

    async def read(self, subject_id: str, part: str | None = None) -> dict[str, Any]:
        row = await self.ctx.db.fetchone("SELECT body_json, parts_json FROM items_rows WHERE subject_id = ?", (subject_id,))  # type: ignore[union-attr]
        if row is None:
            raise NotFound(f"no item {subject_id!r} in the items ingestor")
        parts: dict[str, Any] = json.loads(row["parts_json"])
        parts = parts or {"body": json.loads(row["body_json"])}  # stored before there were parts: the body is the one part
        if part is not None:
            if part not in parts:
                raise NotFound(f"item {subject_id!r} has no part {part!r} (it has: {', '.join(parts)})")
            return {part: parts[part]}
        return parts

    @command("list", "The newest items this ingestor stored, optionally only those with a label, a part, or stored in a date range.", input=ListIn, output=ListOut)
    async def list(self, ctx: PluginOpContext, inp: ListIn) -> ListOut:
        db = self.ctx.db
        assert db is not None
        cond, args = where(inp)
        total = await db.fetchone("SELECT COUNT(*) AS n FROM items_rows" + cond, tuple(args))
        rows = await db.fetchall("SELECT subject_id, source, body_json, parts_json, created_at FROM items_rows" + cond + " ORDER BY created_at DESC, subject_id DESC LIMIT ?", (*args, inp.limit))
        return ListOut(total=int(total["n"]) if total else 0,
                       items=[ItemRow(subject_id=r["subject_id"], source=r["source"], created_at=r["created_at"],
                                      preview=str(json.loads(r["body_json"]))[:80], parts=list(json.loads(r["parts_json"]))) for r in rows])  # fmt: skip
