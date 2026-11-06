# SPDX-License-Identifier: Apache-2.0
"""Schedules: look, add, change, remove and run now. They live in the database, not in config.toml, so a change takes
effect at once and needs no reload. A plugin seeds its own at start (``plugin.<name>.<task>``) only when none of that name
exists, so a plugin schedule you delete comes back at the next start.
"""

import json
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from oaken_core.cron import parse_cron
from oaken_core.errors import Conflict, ConfigError, InvalidInput, NotFound, Unavailable
from oaken_core.operations.context import OpContext
from oaken_core.operations.registry import Change, Preview, operation
from oaken_core.scheduler import Scheduler


class Empty(BaseModel):
    pass


class ScheduleRef(BaseModel):
    name: str = Field(description="The schedule's name, such as plugin.voltpop.kev.")


class ScheduleRow(BaseModel):
    name: str
    kind: str
    queue: str
    every_seconds: float | None
    cron: str | None
    priority: int
    next_run: str
    subject: str | None
    payload: str = Field(description="The job's payload, as JSON text.")
    updated_at: str


def _row(r: dict[str, Any]) -> ScheduleRow:
    interval = r["spec_kind"] == "interval"
    return ScheduleRow(name=r["name"], kind=r["kind"], queue=r["queue"], every_seconds=float(r["spec"]) if interval else None,
                       cron=None if interval else r["spec"], priority=r["priority"], next_run=r["next_run"], subject=r["subject"],
                       payload=r["payload_json"], updated_at=r["updated_at"])  # fmt: skip


def _scheduler(ctx: OpContext) -> Scheduler:
    if ctx.svc.scheduler is None:
        raise Unavailable("the scheduler is not running: start the daemon")
    return ctx.svc.scheduler


async def _must(ctx: OpContext, name: str) -> dict[str, Any]:
    r = await _scheduler(ctx).row(name)
    if r is None:
        raise NotFound(f"no schedule named {name!r}")
    return r


class ScheduleListOut(BaseModel):
    schedules: list[ScheduleRow]


@operation("schedule_list", "Every schedule: what it runs, when, and when it runs next.", permission="read", input=Empty, output=ScheduleListOut, mcp="on", group="schedule")
async def schedule_list(ctx: OpContext, inp: Empty) -> ScheduleListOut:
    return ScheduleListOut(schedules=[_row(r) for r in await _scheduler(ctx).rows()])


@operation("schedule_get", "One schedule.", permission="read", input=ScheduleRef, output=ScheduleRow, mcp="on", group="schedule")
async def schedule_get(ctx: OpContext, inp: ScheduleRef) -> ScheduleRow:
    return _row(await _must(ctx, inp.name))


class ScheduleNewIn(BaseModel):
    name: str = Field(min_length=1, max_length=120, description="The schedule's name.")
    kind: str = Field(min_length=1, max_length=120, description="The job kind it enqueues (plugin.voltpop.run).")
    payload: str = Field(default="{}", description="The job's payload: a JSON object, as text.")
    every_seconds: float | None = Field(default=None, gt=0, description="Run every this many seconds. Give this or cron.")
    cron: str | None = Field(default=None, description="Five-field cron, in UTC. Give this or every_seconds.")
    queue: str = Field(default="default", description="The queue the job goes to.")
    priority: int = Field(default=50, ge=0, le=100)
    subject: str | None = None


class ScheduleEditIn(ScheduleRef):
    model_config = ConfigDict(extra="forbid")
    kind: str | None = None
    payload: str | None = Field(default=None, description="A new payload: a JSON object, as text.")
    every_seconds: float | None = Field(default=None, gt=0, description="Switch to this interval. Giving it replaces a cron.")
    cron: str | None = Field(default=None, description="Switch to this cron (UTC). Giving it replaces an interval.")
    queue: str | None = None
    priority: int | None = Field(default=None, ge=0, le=100)


class ScheduleChangeOut(BaseModel):
    name: str
    next_run: str


class _Raw(BaseModel):  # a payload we only carry: whatever object it was, it goes back out as it came
    model_config = ConfigDict(extra="allow")


def _payload(text: str) -> _Raw:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise InvalidInput(f"payload is not JSON: {exc}") from None
    if not isinstance(data, dict):
        raise InvalidInput("payload must be a JSON object")
    return _Raw(**data)


def _when(ctx: OpContext, every: float | None, cron: str | None) -> tuple[timedelta | None, str | None]:
    if (every is None) == (cron is None):
        raise InvalidInput("give exactly one of every_seconds or cron")
    if cron is not None:
        try:
            parse_cron(cron)
        except ConfigError as exc:
            raise InvalidInput(str(exc)) from None
    return (timedelta(seconds=every) if every is not None else None), cron


async def _put(ctx: OpContext, name: str, kind: str, payload: str, every: float | None, cron: str | None, queue: str, priority: int, subject: str | None) -> ScheduleChangeOut:
    ctx.svc.queues.get(queue)  # NotFound for a queue that is not there
    every_td, cron_text = _when(ctx, every, cron)
    sched = _scheduler(ctx)
    await sched.add(name, kind, _payload(payload), every=every_td, cron=cron_text, priority=priority, subject=subject, queue=queue)
    return ScheduleChangeOut(name=name, next_run=str(await sched.next_run(name)))


async def _new_check(ctx: OpContext, inp: ScheduleNewIn) -> None:
    if await _scheduler(ctx).row(inp.name) is not None:
        raise Conflict(f"schedule {inp.name!r} already exists: use schedule_edit")
    _when(ctx, inp.every_seconds, inp.cron)
    _payload(inp.payload)
    ctx.svc.queues.get(inp.queue)


async def preview_schedule_new(ctx: OpContext, inp: ScheduleNewIn) -> Preview:
    await _new_check(ctx, inp)
    when = f"every {inp.every_seconds:g}s" if inp.every_seconds else f"cron {inp.cron}"
    return Preview(summary=f"Add schedule {inp.name}: {inp.kind} {when}", changes=[Change(target=inp.name, field="schedule", after=when)], total_changes=1)


@operation("schedule_new", "Add a schedule: enqueue a job kind every so often, or by cron (UTC).", permission="mutate", input=ScheduleNewIn, output=ScheduleChangeOut,
           mcp="never", group="schedule", preview=preview_schedule_new)  # fmt: skip
async def schedule_new(ctx: OpContext, inp: ScheduleNewIn) -> ScheduleChangeOut:
    await _new_check(ctx, inp)
    return await _put(ctx, inp.name, inp.kind, inp.payload, inp.every_seconds, inp.cron, inp.queue, inp.priority, inp.subject)


async def _edit_plan(ctx: OpContext, inp: ScheduleEditIn) -> tuple[dict[str, Any], list[Change]]:
    r = await _must(ctx, inp.name)
    if inp.every_seconds is not None and inp.cron is not None:
        raise InvalidInput("give every_seconds or cron, not both")
    new: dict[str, Any] = {k: v for k, v in (("kind", inp.kind), ("payload", inp.payload), ("queue", inp.queue), ("priority", inp.priority)) if v is not None}
    if inp.every_seconds is not None or inp.cron is not None:
        new["every_seconds"], new["cron"] = inp.every_seconds, inp.cron
    if not new:
        raise InvalidInput("nothing to change: give a field")
    old = _row(r).model_dump()
    old["payload"] = r["payload_json"]
    return new, [Change(target=inp.name, field=k, before=old.get(k), after=v) for k, v in new.items()]


async def preview_schedule_edit(ctx: OpContext, inp: ScheduleEditIn) -> Preview:
    new, changes = await _edit_plan(ctx, inp)
    if "cron" in new:
        _when(ctx, new["every_seconds"], new["cron"])
    if "payload" in new:
        _payload(new["payload"])
    return Preview(summary=f"Change schedule {inp.name}: {', '.join(new)}", changes=changes, total_changes=len(changes))


@operation("schedule_edit", "Change a schedule's kind, payload, timing, queue or priority. A new timing recomputes its next run.", permission="mutate", input=ScheduleEditIn,
           output=ScheduleChangeOut, mcp="never", group="schedule", preview=preview_schedule_edit)  # fmt: skip
async def schedule_edit(ctx: OpContext, inp: ScheduleEditIn) -> ScheduleChangeOut:
    new, _ = await _edit_plan(ctx, inp)
    cur = _row(await _must(ctx, inp.name))
    every, cron = (new["every_seconds"], new["cron"]) if "cron" in new else (cur.every_seconds, cur.cron)
    return await _put(ctx, inp.name, new.get("kind", cur.kind), new.get("payload", cur.payload), every, cron, new.get("queue", cur.queue),
                      new.get("priority", cur.priority), cur.subject)  # fmt: skip


async def preview_schedule_delete(ctx: OpContext, inp: ScheduleRef) -> Preview:
    r = await _must(ctx, inp.name)
    return Preview(summary=f"Remove schedule {inp.name}" + (" (a plugin seeds it again at the next start)" if inp.name.startswith("plugin.") else ""),
                   changes=[Change(target=inp.name, field="schedule", before=r["kind"], after=None)], total_changes=1)  # fmt: skip


@operation("schedule_delete", "Remove a schedule. A plugin's own comes back at the next start.", permission="mutate", input=ScheduleRef, output=ScheduleRef,
           mcp="never", group="schedule", preview=preview_schedule_delete)  # fmt: skip
async def schedule_delete(ctx: OpContext, inp: ScheduleRef) -> ScheduleRef:
    await _must(ctx, inp.name)
    await _scheduler(ctx).remove(inp.name)
    return inp


async def preview_schedule_run(ctx: OpContext, inp: ScheduleRef) -> Preview:
    r = await _must(ctx, inp.name)
    return Preview(summary=f"Run schedule {inp.name} now ({r['kind']}); its next run does not move", changes=[Change(target=inp.name, field="job", after=r["kind"])], total_changes=1)


@operation("schedule_run", "Run a schedule once, now. Its next run does not move.", permission="mutate", input=ScheduleRef, output=ScheduleRef, mcp="never", group="schedule",
           preview=preview_schedule_run)  # fmt: skip
async def schedule_run(ctx: OpContext, inp: ScheduleRef) -> ScheduleRef:
    await _must(ctx, inp.name)
    await _scheduler(ctx).run_now(inp.name)
    return inp


OPERATIONS = (schedule_list, schedule_get, schedule_new, schedule_edit, schedule_delete, schedule_run)
