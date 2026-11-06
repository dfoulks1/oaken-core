# SPDX-License-Identifier: Apache-2.0
"""Recurring work: fixed intervals and cron expressions, persisted in ``raw.db``.

Each schedule stores its ``next_run``. ``tick()`` enqueues every due schedule and moves its
``next_run`` forward **in the same transaction**, so a crash can neither fire a run twice nor lose
it. The enqueued job also carries a dedupe key naming the slot, as a second guard.

If the process was down for several periods, the missed runs collapse into one: a schedule fires
once on the next tick and then resumes its normal rhythm (no catch-up storm). Intervals keep their
phase (``anchor + k * interval``); cron expressions are evaluated in UTC (see ``cron.py``).
"""

import asyncio
import contextlib
from datetime import datetime, timedelta

from pydantic import BaseModel

from oaken_core.clock import Clock, fmt_ts, parse_ts
from oaken_core.cron import parse_cron
from oaken_core.db import Database
from oaken_core.errors import ConfigError
from oaken_core.jobs import PRIORITY_INGEST, Jobs
from oaken_core.queues import DEFAULT

MAX_SLEEP_SECONDS = 30.0


def _next_run(spec_kind: str, spec: str, after: datetime, anchor: datetime) -> datetime:
    """The first run strictly after ``after``."""
    if spec_kind == "cron":
        return parse_cron(spec).next_after(after)
    step = float(spec)
    periods = int((after - anchor).total_seconds() // step) + 1
    return anchor + timedelta(seconds=periods * step)


class Scheduler:
    def __init__(self, db: Database, jobs: Jobs, clock: Clock) -> None:
        self.db = db
        self.jobs = jobs
        self.clock = clock
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def add(
        self,
        name: str,
        kind: str,
        payload: BaseModel,
        *,
        every: timedelta | None = None,
        cron: str | None = None,
        priority: int = PRIORITY_INGEST,
        subject: str | None = None,
        queue: str = DEFAULT,
    ) -> None:
        """Create or update a schedule (idempotent: call it at every start-up). Re-adding an
        unchanged schedule keeps its ``next_run``; changing the spec recomputes it."""
        if (every is None) == (cron is None):
            raise ConfigError("give exactly one of every= or cron=")
        if every is not None:
            if every.total_seconds() <= 0:
                raise ConfigError("every= must be positive")
            spec_kind, spec = "interval", str(every.total_seconds())
        else:
            spec_kind, spec = "cron", cron or ""
            parse_cron(spec)  # validate now, not at the first tick
        now = self.clock.now()
        async with self.db.transaction() as tx:
            row = await tx.fetchone(
                "SELECT spec_kind, spec, next_run FROM schedules WHERE name = ?", (name,)
            )
            unchanged = row and (row["spec_kind"], row["spec"]) == (spec_kind, spec)
            next_run = (
                parse_ts(row["next_run"])
                if unchanged and row
                else _next_run(spec_kind, spec, now, now)
            )
            await tx.execute(
                "INSERT INTO schedules (name, subject, queue, kind, payload_json, spec_kind, spec,"
                " priority, next_run, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET subject = excluded.subject,"
                " queue = excluded.queue,"
                " kind = excluded.kind, payload_json = excluded.payload_json,"
                " spec_kind = excluded.spec_kind, spec = excluded.spec,"
                " priority = excluded.priority, next_run = excluded.next_run,"
                " updated_at = excluded.updated_at",
                (
                    name,
                    subject,
                    queue,
                    kind,
                    payload.model_dump_json(),
                    spec_kind,
                    spec,
                    priority,
                    fmt_ts(next_run),
                    fmt_ts(now),
                ),
            )

    async def remove(self, name: str) -> bool:
        return await self.db.execute("DELETE FROM schedules WHERE name = ?", (name,)) > 0

    async def next_run(self, name: str) -> datetime | None:
        row = await self.db.fetchone("SELECT next_run FROM schedules WHERE name = ?", (name,))
        return parse_ts(row["next_run"]) if row else None

    async def tick(self) -> int:
        """Enqueue every schedule that is due now. Returns how many fired."""
        now = self.clock.now()
        changes = []
        async with self.db.transaction() as tx:
            due = await tx.fetchall(
                "SELECT * FROM schedules WHERE next_run <= ? ORDER BY next_run, name",
                (fmt_ts(now),),
            )
            for r in due:
                slot = r["next_run"]
                _, change = await self.jobs.enqueue_in(
                    tx,
                    r["kind"],
                    r["payload_json"],
                    queue=r["queue"],
                    subject=r["subject"],
                    priority=r["priority"],
                    dedupe_key=f"schedule:{r['name']}:{slot}",
                )
                changes.append(change)
                nxt = _next_run(r["spec_kind"], r["spec"], now, parse_ts(slot))
                await tx.execute(
                    "UPDATE schedules SET next_run = ?, updated_at = ? WHERE name = ?",
                    (fmt_ts(nxt), fmt_ts(now), r["name"]),
                )
        await self.jobs.announce(*changes)
        return len(due)

    # -- running in the background ----------------------------------------------------------
    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("scheduler already started")
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task:
            await task

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self.tick()
            row = await self.db.fetchone("SELECT MIN(next_run) AS t FROM schedules")
            timeout = MAX_SLEEP_SECONDS
            if row and row["t"]:
                wait = (parse_ts(row["t"]) - self.clock.now()).total_seconds()
                timeout = min(MAX_SLEEP_SECONDS, max(wait, 0.05))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout)
