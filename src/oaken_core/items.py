# SPDX-License-Identifier: Apache-2.0
"""The built-in ingestor: a table of what was submitted.

Core has no nouns, so something has to say where an accepted item goes. A product supplies that (a CRM's
contacts, a mail client's messages). This one is for plain ``oaken``: it keeps ``(subject, source, body)`` rows so
an item submitted with ``oaken intake submit`` has somewhere to land and can be read back with ``oaken items list``.
It is a fallback: the moment a product installs an ingestor of its own, this one steps aside.
"""

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from oaken_core.errors import NotFound, PluginError
from oaken_core.ids import SubjectId, new_subject_id
from oaken_core.plugins.base import BaseIngestor, command
from oaken_core.plugins.contracts import PluginContext, PluginOpContext, Query


COLLECTION = "items"


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


def query(inp: ListIn) -> Query:
    return Query(has=tuple(f"parts.{n}" for n in inp.part),
                 labels=tuple((k, v if eq else None) for k, eq, v in (lab.partition("=") for lab in inp.label)),
                 since=inp.since, until=inp.until, newest_first=True, limit=inp.limit)  # fmt: skip


class ItemsIngestor(BaseIngestor):
    api_version = 1  # the plugin API this was written against (core's PLUGIN_API_VERSION)
    requires_core = ">=0.0.0"  # the oaken-core versions it works with
    version: ClassVar[str] = "1"
    fallback: ClassVar[bool] = True
    migrations_dir: ClassVar[Path | None] = Path(__file__).parent / "items_migrations"  # only moves old rows into the store

    async def start(self, ctx: PluginContext) -> None:
        await super().start(ctx)
        if ctx.store is None:
            raise PluginError("items: no store to keep items in")

    async def ingest(self, item: Any) -> SubjectId:
        parts = {n: p.content for n, p in item.parts.items()} or {"body": item.body}  # no formatter: the body is the one part
        rec = await self.ctx.store.create(COLLECTION, {"source": item.source, "body": item.body, "parts": parts}, id=new_subject_id())
        return SubjectId(rec.id)

    async def read(self, subject_id: str, part: str | None = None) -> dict[str, Any]:
        try:
            rec = await self.ctx.store.get(COLLECTION, subject_id)
        except NotFound:
            raise NotFound(f"no item {subject_id!r} in the items ingestor") from None
        parts: dict[str, Any] = rec.data["parts"]
        if part is not None:
            if part not in parts:
                raise NotFound(f"item {subject_id!r} has no part {part!r} (it has: {', '.join(parts)})")
            return {part: parts[part]}
        return parts

    @command("list", "The newest items this ingestor stored, optionally only those with a label, a part, or stored in a date range.", input=ListIn, output=ListOut)
    async def list(self, ctx: PluginOpContext, inp: ListIn) -> ListOut:
        store, q = self.ctx.store, query(inp)
        recs = await store.list(COLLECTION, q)
        return ListOut(total=await store.count(COLLECTION, q),
                       items=[ItemRow(subject_id=r.id, source=r.data["source"], created_at=r.created_at,
                                      preview=str(r.data["body"])[:80], parts=list(r.data["parts"])) for r in recs])  # fmt: skip
