# SPDX-License-Identifier: Apache-2.0
"""Housekeeping a person can see and ask for: storage figures, vacuum, the answer cache and the recent
event history. Lifted from oaken-mail ``builtin/maintenance.py`` (cf3b187), on core's tables.

Sizes come from the filesystem. The cache is derived data (model answers keyed by what produced them),
so clearing it costs only the model calls it saved. None of these is offered over MCP.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from oaken_core import paths
from oaken_core.errors import Conflict, StorageError
from oaken_core.maintenance import check_room, enqueue_vacuum, file_bytes
from oaken_core.operations.context import OpContext
from oaken_core.operations.registry import AnyOperation, Change, Preview, operation
from oaken_core.router.store import AnswerCache


class Empty(BaseModel):
    pass


# -- storage -----------------------------------------------------------------------------------
class StoredFile(BaseModel):
    name: str
    bytes: int


class StorageInfo(BaseModel):
    data_dir: str
    core_db: str
    core_db_bytes: int
    wal_bytes: int  # the write-ahead log: space a checkpoint or vacuum returns
    files: list[StoredFile] = Field(default_factory=list)  # every other database in the folder, plugins' too


@operation("storage_info", "Where core keeps its data and how big it is: the database, its write-ahead log, "
           "and the other files in the data folder.",
           permission="read", input=Empty, output=StorageInfo, mcp="never", group="storage")  # fmt: skip
async def storage_info(ctx: OpContext, inp: Empty) -> StorageInfo:
    db = ctx.svc.db.path
    data = paths.data_dir()
    files = sorted((p for p in data.glob("*.db") if p != db), key=lambda p: p.name)
    return StorageInfo(data_dir=str(db.parent), core_db=str(db), core_db_bytes=file_bytes(db),
                       wal_bytes=file_bytes(Path(f"{db}-wal")),
                       files=[StoredFile(name=p.name, bytes=file_bytes(p)) for p in files])  # fmt: skip


class QueuedOut(BaseModel):
    job_id: str
    created: bool  # False: one was already queued or running, and this is it


async def _preview_vacuum(ctx: OpContext, inp: Empty) -> Preview:
    return Preview(summary="Queue a vacuum of core's database", changes=[Change(target="core.db", field="vacuum")], total_changes=1)


@operation("storage_vacuum", "Rewrite core's database to give back the space deleted rows left. One at a time. It holds "
           "the write lock while it works (writers wait, readers go on) and needs about the database's size free on disk.",
           permission="mutate", input=Empty, output=QueuedOut, mcp="never", group="storage", streaming=True,
           preview=_preview_vacuum)  # fmt: skip
async def storage_vacuum(ctx: OpContext, inp: Empty) -> QueuedOut:
    try:
        check_room(ctx.svc.db)
    except StorageError as exc:
        raise Conflict(str(exc)) from None
    queued = await enqueue_vacuum(ctx.svc.queue, actor=ctx.caller.actor)
    return QueuedOut(job_id=queued.job_id, created=queued.created)


# -- the answer cache --------------------------------------------------------------------------
class CacheStats(BaseModel):
    entries: int
    bytes: int  # the stored answers, not the file around them
    by_analyzer: dict[str, int] = Field(default_factory=dict)
    oldest: datetime | None = None


def _cache(ctx: OpContext) -> Any:
    """The cache the router uses right now (a plugin's, or core's own)."""
    router = ctx.svc.router
    return router.cache if router is not None else AnswerCache(ctx.svc.db, ctx.svc.clock)


@operation("cache_stats", "The model router's answer cache: how many answers it holds, how big, and whose.",
           permission="read", input=Empty, output=CacheStats, mcp="never", group="cache")  # fmt: skip
async def cache_stats(ctx: OpContext, inp: Empty) -> CacheStats:
    s = await _cache(ctx).stats()
    return CacheStats(entries=s.entries, bytes=s.bytes, by_analyzer=dict(s.by_analyzer), oldest=s.oldest)


class ClearCacheIn(BaseModel):
    analyzer: str | None = Field(default=None, description="Only this analyzer's answers; default: all.")


class ClearCacheOut(BaseModel):
    deleted: int


async def _preview_clear(ctx: OpContext, inp: ClearCacheIn) -> Preview:
    stats = await _cache(ctx).stats()
    n = stats.by_analyzer.get(inp.analyzer, 0) if inp.analyzer else stats.entries
    return Preview(summary=f"Delete {n} cached answer(s)" + (f" of {inp.analyzer}" if inp.analyzer else ""),
                   changes=[Change(target=inp.analyzer or "all", field="answer_cache", before=n, after=0)], total_changes=n)  # fmt: skip


@operation("cache_clear", "Delete cached model answers (all, or one analyzer's). Only derived results go: anything cleared is "
           "recomputed when asked again, at the cost of the model calls the cache had saved.",
           permission="mutate", input=ClearCacheIn, output=ClearCacheOut, mcp="never", group="cache",
           preview=_preview_clear)  # fmt: skip
async def cache_clear(ctx: OpContext, inp: ClearCacheIn) -> ClearCacheOut:
    return ClearCacheOut(deleted=await _cache(ctx).clear(inp.analyzer))


# -- the event history -------------------------------------------------------------------------
class EventsIn(BaseModel):
    limit: int = Field(default=100, ge=1, le=1000)
    since: datetime | None = None  # nothing older than this
    kinds: list[str] | None = None  # only these kinds (JobStateChanged, PluginHealthChanged, ...)


class EventRow(BaseModel):
    ts: datetime
    kind: str
    data: dict[str, Any]  # the event's fields: ids, states and numbers, never the owner's text


class EventsOut(BaseModel):
    events: list[EventRow]  # oldest first


@operation("events_tail", "The most recent events core published, from its event log: what happened before this page was "
           "opened, or before core was last restarted. Ids, states and numbers, never text.",
           permission="read", input=EventsIn, output=EventsOut, mcp="never", group="events")  # fmt: skip
async def events_tail(ctx: OpContext, inp: EventsIn) -> EventsOut:
    rows = ctx.svc.need_eventlog().tail(inp.limit, since=inp.since, kinds=set(inp.kinds) if inp.kinds else None)
    return EventsOut(events=[EventRow.model_validate(r) for r in rows])


OPERATIONS: tuple[AnyOperation, ...] = (storage_info, storage_vacuum, cache_stats, cache_clear, events_tail)
