# SPDX-License-Identifier: Apache-2.0
"""Intake: the first stage. Triage runs BEFORE anything is stored, so junk never reaches the store.

``submit`` puts an item in the ``default`` queue as an ``intake.triage`` job. It holds the payload
and a provisional subject id; nothing else exists yet. Triagers (lowest ``rank`` first, the first
verdict wins) label the provisional subject, including ``queue`` and ``priority``. Then:

- reject: the payload is dropped and one content-free change-log row is written (time, source,
  verdict, reason, a hash of the payload). The labels are cleared. Nothing is stored.
- accept or unsure: ``ingest(item)`` stores it and returns the real ``SubjectId`` (a domain plugin
  provides it); the labels move to that subject. A human then accepts or declines unsure ones.

A failing triager lets intake back up: the job retries with backoff, then stays failed with its
payload intact, and ``requeue`` replays it once the triager is fixed. Core never purges intake.
``open_gate_on_final_failure`` (off by default) instead treats the final failure as "unsure".
"""

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from oaken_core.actors import SYSTEM, Actor
from oaken_core.changelog import ChangeLog
from oaken_core.db import Database
from oaken_core.errors import Unavailable
from oaken_core.ids import SubjectId, new_subject_id
from oaken_core.jobs import Job, JobQueue, JobRunner, PermanentJobError
from oaken_core.labels import LabelError, LabelStore
from oaken_core.logging import get_logger
from oaken_core.queues import DEFAULT

log = get_logger("oaken_core.intake")

KIND = "intake.triage"


class IntakePayload(BaseModel):
    subject_id: str  # provisional: the item has no record yet
    source: str
    body: Any = None
    labels: dict[str, str] = Field(default_factory=dict)  # set at submit, e.g. confidential


class IntakeResult(BaseModel):
    verdict: str
    subject_id: str | None = None  # the real subject, when the item was ingested


@dataclass(frozen=True)
class IntakeItem:
    subject_id: str
    source: str
    body: Any
    labels: dict[str, str]


@dataclass(frozen=True)
class Verdict:
    kind: Literal["accept", "reject", "unsure"]
    reason: str = ""
    labels: dict[str, str] = field(default_factory=dict)  # written to the provisional subject


class Triager(Protocol):
    name: str
    rank: int  # lowest first: deterministic rules (10) before a model (90)

    async def triage(self, item: IntakeItem) -> Verdict | None:
        """A verdict, or None to pass to the next triager."""
        ...


IngestFn = Callable[[IntakeItem], Awaitable[SubjectId]]


class Intake:
    def __init__(
        self,
        queue: JobQueue,
        labels: LabelStore,
        db: Database,
        changelog: ChangeLog,
        *,
        triagers: Sequence[Triager] = (),
        ingest: IngestFn | None = None,
    ) -> None:
        self.queue, self.labels, self.db, self.changelog = queue, labels, db, changelog
        self.triagers = sorted(triagers, key=lambda t: (t.rank, t.name))
        self.ingest = ingest

    def register(self, runner: JobRunner) -> None:
        runner.register(
            KIND, IntakePayload, self._triage_job, result_model=IntakeResult, scrub_payload=True
        )

    async def submit(
        self, source: str, body: Any, *, labels: dict[str, str] | None = None, actor: Actor = SYSTEM
    ) -> SubjectId:
        """Queue an item for triage. Returns its provisional subject id. ``labels`` are set before
        triage (a provider marks ``confidential`` here, so a remote triager never sees it)."""
        subject = new_subject_id()
        for key, value in (labels or {}).items():
            await self.labels.set(subject, key, value, actor)
        payload = IntakePayload(subject_id=subject, source=source, body=body, labels=labels or {})
        await self.queue.enqueue(KIND, payload, subject=subject, queue=DEFAULT, actor=actor)
        return subject

    async def _triage_job(self, p: IntakePayload, job: Job) -> IntakeResult:
        item = IntakeItem(p.subject_id, p.source, p.body, p.labels)
        who, verdict = SYSTEM, None
        try:
            for t in self.triagers:
                verdict = await t.triage(item)
                if verdict is not None:
                    who = Actor("system", t.name)
                    break
        except Exception:
            if not (
                self.queue.settings.open_gate_on_final_failure and job.attempts >= job.max_attempts
            ):
                raise  # intake backs up; requeue replays it
            log.warning("triager failed on the last attempt; treating as unsure", job_id=job.id)
            verdict = Verdict("unsure", "triage failed")
        if verdict is None:
            verdict = Verdict("unsure", "no triager decided")
        try:
            for key, value in verdict.labels.items():
                await self.labels.set(p.subject_id, key, value, who)
        except LabelError as exc:  # a triager bug: a retry would repeat it
            raise PermanentJobError(str(exc)) from exc
        if verdict.kind == "reject":
            await self._reject(item, verdict, who)
            return IntakeResult(verdict="reject")
        if self.ingest is None:
            raise Unavailable("intake has no ingest hook; nothing can store this item yet")
        real = await self.ingest(item)
        await self.labels.move(p.subject_id, real)
        async with self.db.transaction() as tx:
            await self.changelog.append(
                tx, actor=who, subject_id=real, key="intake", op="ingest",
                new=json.dumps({"from": p.subject_id, "source": p.source, "verdict": verdict.kind,
                                "reason": verdict.reason}),
            )  # fmt: skip
        return IntakeResult(verdict=verdict.kind, subject_id=real)

    async def _reject(self, item: IntakeItem, verdict: Verdict, who: Actor) -> None:
        """Drop the item. The log row holds no content: only a hash, so a mistake can be
        recognised later without keeping the text."""
        digest = hashlib.sha256(json.dumps(item.body, sort_keys=True, default=str).encode()).hexdigest()
        async with self.db.transaction() as tx:
            await self.changelog.append(
                tx, actor=who, subject_id=item.subject_id, key="intake", op="reject",
                new=json.dumps({"source": item.source, "reason": verdict.reason, "sha256": digest}),
            )  # fmt: skip
        await self.labels.clear_machine(item.subject_id, who)
        await self.labels.clear_human_all(item.subject_id, who)
