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
from oaken_core.errors import PluginError
from oaken_core.ids import SubjectId, new_subject_id
from oaken_core.plugins.base import BaseIngestor, command
from oaken_core.plugins.contracts import PluginContext, PluginOpContext


class ListIn(BaseModel):
    limit: int = Field(default=20, ge=1, le=200)


class ItemRow(BaseModel):
    subject_id: str
    source: str
    created_at: str
    preview: str  # the first characters of the body, enough to recognise it
    parts: list[str]  # the names of the parts a formatter made of it


class ListOut(BaseModel):
    total: int
    items: list[ItemRow]


class ItemsIngestor(BaseIngestor):
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

    @command("list", "The newest items this ingestor stored.", input=ListIn, output=ListOut)
    async def list(self, ctx: PluginOpContext, inp: ListIn) -> ListOut:
        db = self.ctx.db
        assert db is not None
        total = await db.fetchone("SELECT COUNT(*) AS n FROM items_rows")
        rows = await db.fetchall("SELECT subject_id, source, body_json, parts_json, created_at FROM items_rows ORDER BY created_at DESC, subject_id DESC LIMIT ?", (inp.limit,))
        return ListOut(total=int(total["n"]) if total else 0,
                       items=[ItemRow(subject_id=r["subject_id"], source=r["source"], created_at=r["created_at"],
                                      preview=str(json.loads(r["body_json"]))[:80], parts=list(json.loads(r["parts_json"]))) for r in rows])  # fmt: skip
