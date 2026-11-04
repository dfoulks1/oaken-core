# SPDX-License-Identifier: Apache-2.0
"""Durable job queues and the worker runtime.

Adapted from oaken-mail ``core/jobs.py`` (cf3b187). ``account_id`` became ``subject``; jobs live in
named queues (see ``queues.py``); the mail quota and external-agent lease code is left out.

``JobQueue`` is storage: every state change is one short transaction, so a crash leaves a job
queued, or running with a lease that expires and is reclaimed. ``JobRunner`` runs N asyncio workers
per queue. Delivery is **at least once**: handlers must be idempotent. Times are UTC fixed-width ISO
strings. Retry delay after the Nth failed attempt is ``min(backoff_max, backoff_base * 2**(N-1))``,
reduced by up to ``backoff_jitter`` at random so retries do not arrive in lockstep.

A job's queue is decided when it is enqueued: named directly, or taken from the subject's ``queue``
label, or ``default``. shortcut: relabelling a subject does not move jobs already queued; use
``requeue(to=...)``.
"""

import asyncio
import contextlib
import random
import re
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from sqlite3 import Row
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from oaken_core.actors import SYSTEM, Actor
from oaken_core.changelog import ChangeLog
from oaken_core.clock import Clock, fmt_ts, parse_ts
from oaken_core.db import Database, Tx
from oaken_core.errors import InvalidInput, InvalidResult, NotFound, OakenError
from oaken_core.ids import JobId, new_job_id, uuid7
from oaken_core.labels import LabelStore
from oaken_core.logging import get_logger, log_context
from oaken_core.queues import DEFAULT, Queues
from oaken_core.settings import JobsSettings

log = get_logger("oaken_core.jobs")

M = TypeVar("M", bound=BaseModel)

PRIORITY_USER = 100  # something the user just asked for
PRIORITY_INGEST = 50
PRIORITY_BACKFILL = 10

MAX_ERROR_CHARS = 500
_SECRET_TEXT = re.compile(
    r"""(?ix)
    \b(?P<name>[\w-]*(?:token|secret|passw(?:or)?d|api[_-]?key|authorization|credential)[\w-]*)
    ["']?\s*[=:]\s*["']?(?:bearer\s+)?[^\s"',}]+
    | \bbearer\s+[\w.~+/=-]+
    """
)


class PermanentJobError(OakenError):
    """Raise from a handler to fail the job at once, without retries."""

    code = "job_permanent_failure"


def scrub_error(error: BaseException | str) -> str:
    """Error text safe to store: secrets redacted, then truncated."""
    text = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else error

    def mask(m: re.Match[str]) -> str:
        name = m.group("name")
        return f"{name}=[REDACTED]" if name else "Bearer [REDACTED]"

    text = _SECRET_TEXT.sub(mask, text)
    if len(text) > MAX_ERROR_CHARS:
        return f"{text[:MAX_ERROR_CHARS]}...[truncated {len(text) - MAX_ERROR_CHARS} chars]"
    return text


@dataclass(frozen=True)
class Job:
    id: JobId
    subject: str | None
    queue: str
    kind: str
    payload_json: str
    status: str
    priority: int
    attempts: int
    max_attempts: int
    run_after: datetime
    locked_by: str | None
    locked_until: datetime | None
    cancel_requested: bool
    last_error: str | None
    dedupe_key: str | None
    created_at: datetime
    updated_at: datetime
    parent_job_id: JobId | None
    actor: Actor  # who enqueued it
    result_json: str | None
    completed_by: str | None
    requeued_at: datetime | None
    requeue_count: int

    @classmethod
    def from_row(cls, r: Row) -> "Job":
        return cls(
            id=JobId(r["id"]), subject=r["subject"], queue=r["queue"], kind=r["kind"],
            payload_json=r["payload_json"], status=r["status"], priority=r["priority"],
            attempts=r["attempts"], max_attempts=r["max_attempts"],
            run_after=parse_ts(r["run_after"]), locked_by=r["locked_by"],
            locked_until=parse_ts(r["locked_until"]) if r["locked_until"] else None,
            cancel_requested=bool(r["cancel_requested"]), last_error=r["last_error"],
            dedupe_key=r["dedupe_key"], created_at=parse_ts(r["created_at"]),
            updated_at=parse_ts(r["updated_at"]),
            parent_job_id=JobId(r["parent_job_id"]) if r["parent_job_id"] else None,
            actor=Actor(r["actor_kind"], r["actor_label"]),  # type: ignore[arg-type]
            result_json=r["result_json"], completed_by=r["completed_by"],
            requeued_at=parse_ts(r["requeued_at"]) if r["requeued_at"] else None,
            requeue_count=r["requeue_count"],
        )  # fmt: skip


@dataclass(frozen=True)
class Enqueued:
    job_id: JobId
    created: bool  # False: an equal dedupe_key was already queued or running


@dataclass(frozen=True)
class Selector:
    """Which jobs a requeue touches. At least one field is required: there is no "everything"."""

    job_id: str | None = None
    status: str | None = None
    queue: str | None = None
    kind: str | None = None
    subject: str | None = None

    def empty(self) -> bool:
        return not any((self.job_id, self.status, self.queue, self.kind, self.subject))


@dataclass(frozen=True)
class RequeueResult:
    requeued: tuple[JobId, ...]
    cooling: tuple[JobId, ...]  # skipped: requeued too recently


@dataclass(frozen=True)
class QueueStats:
    queue: str
    paused: bool
    queued: int
    running: int
    failed: int
    oldest_queued_seconds: float | None  # age of the longest-waiting queued job
    last_error: str | None


REQUEUEABLE = ("queued", "failed", "cancelled")


class JobQueue:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        queues: Queues,
        *,
        labels: LabelStore | None = None,
        changelog: ChangeLog | None = None,
        settings: JobsSettings | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.queues = queues
        self.labels = labels
        self.changelog = changelog or ChangeLog(clock)
        self.settings = settings or JobsSettings()
        self.result_models: dict[str, type[BaseModel]] = {}  # a job's result needs a schema
        self._rng = rng or random.Random()  # noqa: S311 (retry jitter, not security)
        self.wake = asyncio.Event()  # set whenever work may have become runnable

    # -- producing --------------------------------------------------------------------------
    async def route(self, subject: str | None, queue: str | None = None) -> str:
        """The queue a job goes to: the one named, else the subject's ``queue`` label, else
        ``default``. Raises ``InvalidInput`` for a queue that does not exist."""
        if queue is None and subject is not None and self.labels is not None:
            queue = await self.labels.get(subject, "queue")
        return self.queues.get(queue or DEFAULT).name

    async def enqueue(
        self,
        kind: str,
        payload: BaseModel,
        *,
        subject: str | None = None,
        queue: str | None = None,
        priority: int = PRIORITY_INGEST,
        dedupe_key: str | None = None,
        run_after: datetime | None = None,
        max_attempts: int | None = None,
        actor: Actor = SYSTEM,
        parent_job_id: JobId | None = None,
    ) -> Enqueued:
        """Queue a job. With a ``dedupe_key`` that is already queued or running, nothing is added
        and the existing job is returned (``created=False``)."""
        where = await self.route(subject, queue)
        async with self.db.transaction() as tx:
            result = await self.enqueue_in(
                tx, kind, payload, queue=where, subject=subject, priority=priority,
                dedupe_key=dedupe_key, run_after=run_after, max_attempts=max_attempts,
                actor=actor, parent_job_id=parent_job_id,
            )  # fmt: skip
        self.wake.set()
        return result

    async def enqueue_in(
        self,
        tx: Tx,
        kind: str,
        payload: BaseModel | str,
        *,
        queue: str,
        subject: str | None = None,
        priority: int = PRIORITY_INGEST,
        dedupe_key: str | None = None,
        run_after: datetime | None = None,
        max_attempts: int | None = None,
        actor: Actor = SYSTEM,
        parent_job_id: JobId | None = None,
    ) -> Enqueued:
        """``enqueue`` inside the caller's transaction, with the queue already resolved
        (``payload`` may be already-serialised JSON). Wake the runner after it commits."""
        if dedupe_key is not None:
            row = await tx.fetchone(
                "SELECT id FROM jobs WHERE dedupe_key = ? AND status IN ('queued', 'running')",
                (dedupe_key,),
            )
            if row:
                return Enqueued(JobId(row["id"]), created=False)
        if parent_job_id is not None and not await tx.fetchone(
            "SELECT 1 FROM jobs WHERE id = ?", (parent_job_id,)
        ):
            raise NotFound(f"no such parent job: {parent_job_id}")
        now = self.clock.now()
        job_id = new_job_id()
        await tx.execute(
            "INSERT INTO jobs (id, subject, queue, kind, payload_json, priority, max_attempts,"
            " run_after, dedupe_key, created_at, updated_at, parent_job_id, actor_kind,"
            " actor_label) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id, subject, queue, kind,
                payload if isinstance(payload, str) else payload.model_dump_json(),
                priority, max_attempts or self.settings.max_attempts,
                fmt_ts(run_after or now), dedupe_key, fmt_ts(now), fmt_ts(now),
                parent_job_id, actor.kind, actor.label,
            ),
        )  # fmt: skip
        return Enqueued(job_id, created=True)

    # -- claiming ---------------------------------------------------------------------------
    async def claim(
        self,
        worker: str,
        queue: str,
        kinds: Collection[str],
        limits: Mapping[str, int] | None = None,
    ) -> Job | None:
        """Atomically take the best runnable job of one of ``kinds`` in ``queue`` and lease it to
        ``worker``. Best = highest priority, then earliest ``run_after``. Kinds already running at
        their ``limits`` cap are skipped. A paused queue yields nothing. Expired leases are
        reclaimed first."""
        now = self.clock.now()
        async with self.db.transaction() as tx:
            await self._reclaim_expired(tx, now)
            paused = await tx.fetchone("SELECT paused FROM queue_state WHERE queue = ?", (queue,))
            if paused and paused["paused"]:
                return None
            allowed = [k for k in kinds if k not in await self._blocked_kinds(tx, limits or {})]
            if not allowed:
                return None
            marks = ",".join("?" * len(allowed))
            row = await tx.fetchone(
                "SELECT * FROM jobs WHERE status = 'queued' AND queue = ? AND run_after <= ?"
                f" AND kind IN ({marks})"  # noqa: S608 (only "?" placeholders)
                " ORDER BY priority DESC, run_after, created_at, id LIMIT 1",
                (queue, fmt_ts(now), *allowed),
            )
            if row is None:
                return None
            lease = now + timedelta(seconds=self.settings.lease_seconds)
            await tx.execute(
                "UPDATE jobs SET status = 'running', locked_by = ?, locked_until = ?,"
                " attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (worker, fmt_ts(lease), fmt_ts(now), row["id"]),
            )
            got = await tx.fetchone("SELECT * FROM jobs WHERE id = ?", (row["id"],))
            assert got is not None
            return Job.from_row(got)

    async def _reclaim_expired(self, tx: Tx, now: datetime) -> None:
        rows = await tx.fetchall(
            "SELECT id, attempts, max_attempts, cancel_requested FROM jobs"
            " WHERE status = 'running' AND locked_until <= ?",
            (fmt_ts(now),),
        )
        for r in rows:
            if r["cancel_requested"]:
                status, error = "cancelled", None
            elif r["attempts"] >= r["max_attempts"]:
                status, error = "failed", "lease expired on the final attempt"
            else:
                status, error = "queued", "lease expired; worker presumed dead"
            await tx.execute(
                "UPDATE jobs SET status = ?, locked_by = NULL, locked_until = NULL,"
                " last_error = COALESCE(?, last_error), run_after = ?, updated_at = ? WHERE id = ?",
                (status, error, fmt_ts(now), fmt_ts(now), r["id"]),
            )

    @staticmethod
    async def _blocked_kinds(tx: Tx, limits: Mapping[str, int]) -> set[str]:
        if not limits:
            return set()
        rows = await tx.fetchall(
            "SELECT kind, COUNT(*) AS n FROM jobs WHERE status = 'running' GROUP BY kind"
        )
        return {r["kind"] for r in rows if r["kind"] in limits and r["n"] >= limits[r["kind"]]}

    # -- finishing --------------------------------------------------------------------------
    async def heartbeat(self, job_id: JobId, worker: str) -> tuple[bool, bool]:
        """Extend the lease. Returns ``(still_owner, cancel_requested)``."""
        now = self.clock.now()
        lease = now + timedelta(seconds=self.settings.lease_seconds)
        async with self.db.transaction() as tx:
            changed = await tx.execute(
                "UPDATE jobs SET locked_until = ?, updated_at = ?"
                " WHERE id = ? AND locked_by = ? AND status = 'running'",
                (fmt_ts(lease), fmt_ts(now), job_id, worker),
            )
            row = await tx.fetchone("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,))
        return changed > 0, bool(row and row["cancel_requested"])

    def register_result(self, kind: str, model: type[BaseModel]) -> None:
        self.result_models[kind] = model

    async def complete(
        self,
        job_id: JobId,
        worker: str,
        result: BaseModel | Mapping[str, Any] | None = None,
        *,
        drop_payload: bool = False,
    ) -> bool:
        """Mark a job succeeded, storing its ``result``. False if the lease was lost. A result is
        validated against its kind's schema and size-limited before anything is stored; a bad one
        raises ``InvalidResult`` and the job stays leased. ``drop_payload`` blanks the payload on
        success (intake: the data now lives in the store, or was rejected and must not be kept)."""
        current = await self.get(job_id)
        if current is None or current.locked_by != worker or current.status != "running":
            return False
        result_json = self._check_result(current.kind, result)
        return await self._finish(
            job_id, worker, "succeeded", result_json=result_json, drop_payload=drop_payload
        )

    def _check_result(self, kind: str, result: BaseModel | Mapping[str, Any] | None) -> str | None:
        model = self.result_models.get(kind)
        if result is None:
            if model is not None:
                raise InvalidResult(f"jobs of kind {kind!r} must return a result")
            return None
        if model is None:
            raise InvalidResult(f"jobs of kind {kind!r} take no result")
        try:
            data = result.model_dump() if isinstance(result, BaseModel) else dict(result)
            text = model.model_validate(data).model_dump_json()
        except ValueError as exc:  # pydantic's ValidationError is a ValueError
            raise InvalidResult(f"result does not match the schema for {kind!r}: {exc}") from exc
        if len(text.encode()) > self.settings.max_result_bytes:
            raise InvalidResult(f"result is larger than {self.settings.max_result_bytes} bytes")
        return text

    async def mark_cancelled(self, job_id: JobId, worker: str) -> bool:
        return await self._finish(job_id, worker, "cancelled")

    async def release(self, job_id: JobId, worker: str) -> bool:
        """Hand a running job back unharmed (shutdown): queued again, attempt not counted."""
        return await self._finish(job_id, worker, "queued", run_now=True, undo_attempt=True)

    async def fail(
        self, job_id: JobId, worker: str, error: BaseException | str, *, permanent: bool = False
    ) -> str:
        """Record a failure. Retries with backoff unless ``permanent`` or out of attempts.
        Returns the new status (``queued`` or ``failed``), or ``lost`` if the lease was lost."""
        now = self.clock.now()
        async with self.db.transaction() as tx:
            row = await tx.fetchone(
                "SELECT * FROM jobs WHERE id = ? AND locked_by = ? AND status = 'running'",
                (job_id, worker),
            )
            if row is None:
                return "lost"
            job = Job.from_row(row)
            if permanent or job.attempts >= job.max_attempts:
                status, run_after = "failed", now
            else:
                status, run_after = "queued", now + self.backoff(job.attempts)
            await tx.execute(
                "UPDATE jobs SET status = ?, run_after = ?, last_error = ?, locked_by = NULL,"
                " locked_until = NULL, updated_at = ? WHERE id = ?",
                (status, fmt_ts(run_after), scrub_error(error), fmt_ts(now), job_id),
            )
        self.wake.set()
        return status

    async def _finish(
        self,
        job_id: JobId,
        worker: str,
        status: str,
        *,
        run_now: bool = False,
        undo_attempt: bool = False,
        result_json: str | None = None,
        drop_payload: bool = False,
    ) -> bool:
        now = fmt_ts(self.clock.now())
        async with self.db.transaction() as tx:
            row = await tx.fetchone(
                "SELECT 1 FROM jobs WHERE id = ? AND locked_by = ? AND status = 'running'",
                (job_id, worker),
            )
            if row is None:
                return False
            await tx.execute(
                "UPDATE jobs SET status = ?, locked_by = NULL, locked_until = NULL,"
                " attempts = attempts - ?, run_after = CASE WHEN ? THEN ? ELSE run_after END,"
                " updated_at = ?, result_json = ?, completed_by = ?,"
                " payload_json = CASE WHEN ? THEN '{}' ELSE payload_json END WHERE id = ?",
                (status, int(undo_attempt), int(run_now), now, now, result_json,
                 worker if status == "succeeded" else None, int(drop_payload), job_id),
            )  # fmt: skip
        self.wake.set()
        return True

    def backoff(self, attempts: int) -> timedelta:
        s = self.settings
        delay = min(s.backoff_max_seconds, s.backoff_base_seconds * 2 ** max(attempts - 1, 0))
        return timedelta(seconds=delay * (1 - self._rng.uniform(0, s.backoff_jitter)))

    # -- inspecting -------------------------------------------------------------------------
    async def cancel(self, job_id: JobId) -> str:
        """Cancel a job. A queued job is cancelled now; a running one is asked to stop and stays
        ``running`` until its worker acts on it."""
        now = fmt_ts(self.clock.now())
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT status FROM jobs WHERE id = ?", (job_id,))
            if row is None:
                raise NotFound(f"no such job: {job_id}")
            if row["status"] == "queued":
                await tx.execute(
                    "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ?",
                    (now, job_id),
                )
                return "cancelled"
            if row["status"] == "running":
                await tx.execute(
                    "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ?",
                    (now, job_id),
                )
            return str(row["status"])

    async def get(self, job_id: JobId) -> Job | None:
        row = await self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return Job.from_row(row) if row else None

    async def query(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        queue: str | None = None,
        subject: str | None = None,
        limit: int = 50,
    ) -> list[Job]:
        """The newest jobs matching the filters (newest first)."""
        where, args = _where(status=status, kind=kind, queue=queue, subject=subject)
        args.append(max(1, min(limit, 500)))
        rows = await self.db.fetchall(
            f"SELECT * FROM jobs{where} ORDER BY created_at DESC, id DESC LIMIT ?",  # noqa: S608
            tuple(args),  # only "?" placeholders; the clause is built from fixed column names
        )
        return [Job.from_row(r) for r in rows]

    async def next_due(self) -> datetime | None:
        row = await self.db.fetchone(
            "SELECT MIN(CASE WHEN status = 'queued' THEN run_after ELSE locked_until END) AS t"
            " FROM jobs WHERE status IN ('queued', 'running')"
        )
        return parse_ts(row["t"]) if row and row["t"] else None

    # -- queue control ----------------------------------------------------------------------
    async def pause(self, queue: str) -> None:
        await self._set_paused(queue, True)

    async def resume(self, queue: str) -> None:
        await self._set_paused(queue, False)
        self.wake.set()

    async def _set_paused(self, queue: str, paused: bool) -> None:
        self.queues.get(queue)
        await self.db.execute(
            "INSERT INTO queue_state (queue, paused, updated_at) VALUES (?,?,?)"
            " ON CONFLICT (queue) DO UPDATE SET paused = excluded.paused,"
            " updated_at = excluded.updated_at",
            (queue, int(paused), fmt_ts(self.clock.now())),
        )

    async def stats(self) -> list[QueueStats]:
        """Depth, running, failed, oldest wait and last error for every queue. A growing intake
        with an old oldest-wait is the sign a triager is broken."""
        now = self.clock.now()
        out = []
        for spec in self.queues.all():
            counts = {
                r["status"]: r["n"]
                for r in await self.db.fetchall(
                    "SELECT status, COUNT(*) AS n FROM jobs WHERE queue = ? GROUP BY status",
                    (spec.name,),
                )
            }
            oldest = await self.db.fetchone(
                "SELECT MIN(run_after) AS t FROM jobs WHERE queue = ? AND status = 'queued'",
                (spec.name,),
            )
            err = await self.db.fetchone(
                "SELECT last_error FROM jobs WHERE queue = ? AND last_error IS NOT NULL"
                " ORDER BY updated_at DESC, id DESC LIMIT 1",
                (spec.name,),
            )
            paused = await self.db.fetchone(
                "SELECT paused FROM queue_state WHERE queue = ?", (spec.name,)
            )
            out.append(
                QueueStats(
                    queue=spec.name,
                    paused=bool(paused and paused["paused"]),
                    queued=counts.get("queued", 0),
                    running=counts.get("running", 0),
                    failed=counts.get("failed", 0),
                    oldest_queued_seconds=(
                        max((now - parse_ts(oldest["t"])).total_seconds(), 0.0)
                        if oldest and oldest["t"]
                        else None
                    ),
                    last_error=err["last_error"] if err else None,
                )
            )
        return out

    # -- requeue ----------------------------------------------------------------------------
    async def requeue(
        self,
        selector: Selector,
        actor: Actor,
        *,
        to: str | None = None,
        retriage: bool = False,
    ) -> RequeueResult:
        """Put failed, cancelled or queued jobs back to run now: attempts reset, last error
        cleared. ``to`` moves them to another queue. ``retriage`` sends intake jobs back to
        ``default`` and clears the machine layer of their subject's labels, so the triager decides
        again; the human layer stays. A running or succeeded job is left alone. A job requeued less
        than ``requeue_cooldown_seconds`` ago is skipped and reported. Each requeue writes a
        change-log row."""
        if selector.empty():
            raise InvalidInput("a requeue needs a selector: job id, status, queue, kind or subject")
        if to is not None:
            self.queues.get(to)
        if retriage and (to is not None):
            raise InvalidInput("retriage already chooses the queue; do not also name one")
        if selector.status is not None and selector.status not in REQUEUEABLE:
            raise InvalidInput(f"only {', '.join(REQUEUEABLE)} jobs can be requeued")
        where, args = _where(
            status=selector.status, kind=selector.kind, queue=selector.queue,
            subject=selector.subject, job_id=selector.job_id,
        )  # fmt: skip
        marks = ",".join("?" * len(REQUEUEABLE))
        clause = (where + " AND" if where else " WHERE") + f" status IN ({marks})"
        rows = await self.db.fetchall(
            f"SELECT * FROM jobs{clause} ORDER BY created_at, id",  # noqa: S608
            (*args, *REQUEUEABLE),
        )
        jobs = [Job.from_row(r) for r in rows]
        if retriage:
            bad = [j for j in jobs if not j.kind.startswith("intake.")]
            if bad:
                raise InvalidInput("retriage applies to intake jobs only")
        now = self.clock.now()
        cooldown = timedelta(seconds=self.settings.requeue_cooldown_seconds)
        ready = [j for j in jobs if j.requeued_at is None or now - j.requeued_at >= cooldown]
        cooling = [j.id for j in jobs if j not in ready]
        if retriage and self.labels is not None:
            for subject in {j.subject for j in ready if j.subject}:
                await self.labels.clear_machine(subject, actor)
        done: list[JobId] = []
        async with self.db.transaction() as tx:
            for job in ready:
                dest = DEFAULT if retriage else (to or job.queue)
                n = await tx.execute(
                    "UPDATE jobs SET status = 'queued', queue = ?, attempts = 0, run_after = ?,"
                    " last_error = NULL, locked_by = NULL, locked_until = NULL,"
                    " cancel_requested = 0, updated_at = ?, requeued_at = ?,"
                    " requeue_count = requeue_count + 1"
                    " WHERE id = ? AND status IN ('queued', 'failed', 'cancelled')",
                    (dest, fmt_ts(now), fmt_ts(now), fmt_ts(now), job.id),
                )
                if n:
                    done.append(job.id)
                    await self.changelog.append(
                        tx, actor=actor, subject_id=job.subject or job.id, key="job", op="requeue",
                        old=f"{job.status}@{job.queue}", new=f"queued@{dest}",
                    )  # fmt: skip
        if done:
            self.wake.set()
        return RequeueResult(tuple(done), tuple(cooling))


def _where(**filters: str | None) -> tuple[str, list[object]]:
    cols = {"job_id": "id"}
    parts, args = [], []
    for name, value in filters.items():
        if value is not None:
            parts.append(f"{cols.get(name, name)} = ?")
            args.append(value)
    return ((" WHERE " + " AND ".join(parts)) if parts else ""), args


# -- the runtime ----------------------------------------------------------------------------
Handler = Callable[[Any, Job], Awaitable[BaseModel | None]]


@dataclass(frozen=True)
class _Registration:
    model: type[BaseModel]
    handler: Handler
    concurrency: int | None
    timeout: float | None = None  # seconds; None uses the jobs setting
    scrub_payload: bool = False


@dataclass
class _Running:
    task: "asyncio.Task[BaseModel | None]"
    user_cancelled: bool = False


class JobRunner:
    """Runs jobs from a ``JobQueue`` with each queue's configured number of workers."""

    def __init__(
        self,
        queue: JobQueue,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.queue = queue
        self._sleep = sleep  # used by the lease heartbeat; replaceable in tests
        self._registry: dict[str, _Registration] = {}
        self._running: dict[JobId, _Running] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self._id = uuid7().hex[-8:]
        self.processed = 0

    def register(
        self,
        kind: str,
        model: type[M],
        handler: Callable[[M, Job], Awaitable[BaseModel | None]],
        *,
        concurrency: int | None = None,
        result_model: type[BaseModel] | None = None,
        timeout: float | None = None,
        scrub_payload: bool = False,
    ) -> None:
        """Run ``handler(payload, job)`` for jobs of ``kind``. The payload is validated against
        ``model``; one that does not validate fails at once. ``concurrency`` caps how many of this
        kind run at once (``kind_limits`` in settings wins). ``scrub_payload`` blanks the stored
        payload when the job succeeds."""
        if kind in self._registry:
            raise ValueError(f"a handler for {kind!r} is already registered")
        self._registry[kind] = _Registration(model, handler, concurrency, timeout, scrub_payload)
        if result_model is not None:
            self.queue.register_result(kind, result_model)

    def _limits(self) -> dict[str, int]:
        limits = {k: r.concurrency for k, r in self._registry.items() if r.concurrency}
        return {**limits, **self.queue.settings.kind_limits}

    # -- lifecycle --------------------------------------------------------------------------
    def start(self) -> None:
        """Start each queue's workers. Make queues before this: later ones get none."""
        if self._tasks:
            raise RuntimeError("runner already started")
        self._stopping = False
        for spec in self.queue.queues.all():
            for i in range(spec.workers):
                name = f"{self._id}-{spec.name}-{i}"
                self._tasks.append(asyncio.create_task(self._worker(name, spec.name, False)))

    async def stop(self, grace: float = 30.0) -> None:
        """Stop claiming. In-flight jobs get ``grace`` seconds; any still running are cancelled and
        their leases released, so another run picks them up unharmed."""
        self._stopping = True
        self.queue.wake.set()
        tasks, self._tasks = self._tasks, []
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=grace)
        for t in pending:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def drain(self) -> int:
        """Run every job runnable *now* in every queue (and any it enqueues), then return how many
        ran. Jobs scheduled for later (retry backoff) are left: advance the clock and drain again."""
        before = self.processed
        workers = [
            asyncio.create_task(self._worker(f"{self._id}-d-{spec.name}-{i}", spec.name, True))
            for spec in self.queue.queues.all()
            for i in range(spec.workers)
        ]
        await asyncio.gather(*workers)
        return self.processed - before

    async def cancel(self, job_id: JobId) -> str:
        """Cancel a job; if it is running here, interrupt its handler immediately."""
        status = await self.queue.cancel(job_id)
        entry = self._running.get(job_id)
        if entry and not entry.task.done():
            entry.user_cancelled = True
            entry.task.cancel()
        return status

    # -- workers ----------------------------------------------------------------------------
    async def _worker(self, name: str, queue: str, idle_exit: bool) -> None:
        while not self._stopping:
            self.queue.wake.clear()
            job = await self.queue.claim(name, queue, list(self._registry), self._limits())
            if job is None:
                if idle_exit:
                    return
                await self._idle_wait()
                continue
            await self._execute(job, name)
            self.processed += 1
            self.queue.wake.set()  # a finished job may free a concurrency slot

    async def _idle_wait(self) -> None:
        cap = min(self.queue.settings.lease_seconds / 2, 30.0)
        due = await self.queue.next_due()
        timeout = cap
        if due is not None:
            timeout = min(cap, max((due - self.queue.clock.now()).total_seconds(), 0.25))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.queue.wake.wait(), timeout)

    async def _execute(self, job: Job, worker: str) -> None:
        q = self.queue
        reg = self._registry[job.kind]
        try:
            payload = reg.model.model_validate_json(job.payload_json)
        except ValidationError as exc:
            await q.fail(job.id, worker, f"invalid payload: {exc}", permanent=True)
            return
        with log_context(job_id=str(job.id), job_kind=job.kind, queue=job.queue, attempt=job.attempts):
            task = asyncio.ensure_future(reg.handler(payload, job))  # carries the log context
        entry = _Running(task)
        self._running[job.id] = entry
        beat = asyncio.create_task(self._heartbeat(job, worker, entry))
        timeout = reg.timeout
        if timeout is None:
            timeout = q.settings.handler_timeout_seconds
        try:
            result = await (asyncio.wait_for(task, timeout) if timeout else task)
            if not await q.complete(job.id, worker, result, drop_payload=reg.scrub_payload):
                log.warning("job finished after losing its lease", job_id=job.id, kind=job.kind)
        except TimeoutError:  # a wedged handler must not hold the worker
            log.warning("job timed out", job_id=job.id, kind=job.kind, timeout=timeout)
            await q.fail(job.id, worker, f"handler exceeded {timeout:g}s")
        except asyncio.CancelledError:
            if entry.user_cancelled:
                await q.mark_cancelled(job.id, worker)
                return
            await q.release(job.id, worker)  # shutting down
            raise
        except (PermanentJobError, InvalidResult) as exc:  # a retry would only repeat it
            await q.fail(job.id, worker, exc, permanent=True)
        except Exception as exc:
            log.warning("job failed", job_id=job.id, kind=job.kind, attempt=job.attempts,
                        error=type(exc).__name__)  # fmt: skip
            await q.fail(job.id, worker, exc)
        finally:
            beat.cancel()
            self._running.pop(job.id, None)

    async def _heartbeat(self, job: Job, worker: str, entry: _Running) -> None:
        interval = self.queue.settings.lease_seconds / 3
        while True:
            await self._sleep(interval)
            owner, cancel = await self.queue.heartbeat(job.id, worker)
            if not owner:
                log.warning("lease lost", job_id=job.id, kind=job.kind)
                return
            if cancel:
                entry.user_cancelled = True
                entry.task.cancel()
                return
