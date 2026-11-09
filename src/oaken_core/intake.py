# SPDX-License-Identifier: Apache-2.0
"""Intake: the first stage. Triage runs BEFORE anything is stored, so what is stored is up to the labels.

``submit`` puts an item in the ``default`` queue as an ``intake.triage`` job. It holds the payload
and a provisional subject id; nothing else exists yet. Triagers (lowest ``rank`` first, the first
verdict wins) label the provisional subject, including ``queue`` and ``priority``. Then:

- reject: the payload is dropped and one content-free change-log row is written (time, source,
  verdict, reason, a hash of the payload). The labels are cleared. Nothing is stored.
- accept or unsure: ``ingest(item)`` stores it and returns the real ``SubjectId`` (a domain plugin
  provides it); the labels move to that subject. Storing comes before any embedding or enriching.
  The ``store`` label (true/false) says whether this item is stored; without it ``[jobs] store_by_default``
  decides for a pass, and a reject is not stored. Whoever labels (provider, rule, triager) owns the policy.
  A reject is never passed on, stored or not.

A failing triager lets intake back up: the job retries with backoff, then stays failed with its
payload intact, and ``requeue`` replays it once the triager is fixed. Core never purges intake.
``open_gate_on_final_failure`` (off by default) instead treats the final failure as "unsure".
"""

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from oaken_core.actors import SYSTEM, Actor
from oaken_core.changelog import ChangeLog
from oaken_core.db import Database
from oaken_core.errors import InvalidInput, Unavailable
from oaken_core.ids import SubjectId, new_subject_id
from oaken_core.jobs import Job, Jobs, JobRunner, PermanentJobError
from oaken_core.labels import LabelError, LabelStore
from oaken_core.logging import get_logger
from oaken_core.parts import Part, whole
from oaken_core.queues import DEFAULT, EMBED, STORE

log = get_logger("oaken_core.intake")

FORMAT_KIND = "intake.format"  # in ``default``: split the raw item into parts
ENVELOPE = "oaken"  # the key that marks an envelope: ``{"oaken": 1, "body": ...}``
RERUN_STAGES = ("enrich", "act")  # the stages a stored message can be sent back to; triage needs the raw message, which is not kept
KIND = "intake.triage"  # in a triager stage: judge the parts
STORE_KIND = "intake.store"
EMBED_KIND = "intake.embed"
ITEMS = "items"  # the vector collection a stored item's embedding goes in


class IntakePayload(BaseModel):
    subject_id: str  # provisional: the item has no record yet
    source: str
    body: Any = None
    labels: dict[str, str] = Field(
        default_factory=dict
    )  # set at submit, e.g. confidential
    key: str | None = None  # what makes this the same item again (a message id); a dedupe triager reads it
    format: str | None = None  # what the body is (``email/rfc822``): picks the formatter; none keeps it whole
    parent: str | None = None  # the subject this item was made from (an envelope from inside); None for a first arrival
    hops: int = 0  # how many re-submissions deep it is (an envelope from inside); an outside item starts at 0
    parts: dict[str, Part] = Field(default_factory=dict)  # filled by the format step


class StorePayload(IntakePayload):
    verdict: str
    reason: str = ""
    by: str = "system"  # the triager that decided, for the change log
    routes: list[str] = Field(default_factory=list)  # the action routes the triagers asked for (ADR 0019)


class EmbedPayload(BaseModel):
    subject_id: str  # the real one: the vector is filed under it
    text: str
    confidential: bool = False


class IntakeResult(BaseModel):
    verdict: str
    subject_id: str | None = None  # the real subject, when the item was ingested


@dataclass(frozen=True)
class IntakeItem:
    subject_id: str
    source: str
    body: Any
    labels: dict[str, str]
    key: str | None = None
    parts: dict[str, Part] = field(
        default_factory=dict
    )  # what the formatter made of the body
    verdicts: "dict[str, Verdict]" = field(
        default_factory=dict
    )  # what the triagers before this one said, by name


@dataclass(frozen=True)
class Verdict:
    kind: Literal["accept", "reject", "unsure"]
    reason: str = ""
    labels: dict[str, str] = field(
        default_factory=dict
    )  # written to the provisional subject
    # Reject and learn: embed the item (vector and hash only) and do nothing else. Needs a reject.
    learn: bool = False
    route: tuple[str, ...] = ()  # action routes this item is owed (ADR 0019); empty routes nowhere


class Triager(Protocol):
    name: str
    rank: int  # lowest first: deterministic rules (10) before a model (90)

    async def triage(self, item: IntakeItem) -> Verdict | None:
        """A verdict, or None to pass to the next triager."""
        ...


def item_text(item: IntakeItem) -> str:
    """The text of an item's body, for embedding. An item with no text of its own is its JSON."""
    return (
        item.body
        if isinstance(item.body, str)
        else json.dumps(item.body, sort_keys=True, default=str)
    )


IngestFn = Callable[[IntakeItem], Awaitable[SubjectId]]


class Intake:
    def __init__(
        self,
        jobs: Jobs,
        labels: LabelStore,
        db: Database,
        changelog: ChangeLog,
        *,
        triagers: Sequence[Triager] = (),
        ingest: IngestFn | None = None,
        router: Any = None,
        triager_source: Callable[[str], Sequence[Triager]] | None = None,
        formatter_for: Callable[[str | None], Any] | None = None,
        on_stored: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        self.rerun_hook: Callable[[str, str, str, list[str]], Awaitable[None]] | None = None  # (subject, source, stage, routes): the host rebuilds a stored item and runs a later stage
        self.on_stored = on_stored  # called with a ``StagePayload`` once an item is stored: the enrich and act loops
        self.router = router  # the model router, for ``Verdict.learn``
        self.jobs, self.labels, self.db, self.changelog = jobs, labels, db, changelog
        self.triagers = sorted(triagers, key=lambda t: (t.rank, t.name))
        self.triager_source = triager_source  # asked on every job with the stage's queue name, so a plugin that comes or goes counts
        self.formatter_for = (
            formatter_for  # the formatter for an item's ``format``, or None
        )
        self.ingest = ingest

    def register(self, runner: JobRunner) -> None:
        runner.register(
            FORMAT_KIND, IntakePayload, self._format_job, scrub_payload=True
        )
        runner.register(
            KIND,
            IntakePayload,
            self._triage_job,
            result_model=IntakeResult,
            scrub_payload=True,
        )
        runner.register(
            STORE_KIND,
            StorePayload,
            self._store_job,
            result_model=IntakeResult,
            scrub_payload=True,
        )
        runner.register(EMBED_KIND, EmbedPayload, self._embed_job, scrub_payload=True)

    async def submit(
        self,
        source: str,
        body: Any,
        *,
        labels: dict[str, str] | None = None,
        key: str | None = None,
        format: str | None = None,
        actor: Actor = SYSTEM,
        trusted: bool = False,
    ) -> SubjectId:
        """Queue an item for triage. Returns its provisional subject id. ``labels`` are set before
        triage (a provider marks ``confidential`` here, so a remote triager never sees it). ``key`` is the
        source's own id for the item, for a triager that drops repeats."""
        parent, hops = None, 0
        if isinstance(body, dict) and body.get(ENVELOPE) == 1 and "body" in body:  # an envelope: unwrap it
            env, body = body, body["body"]
            format, key = format or env.get("format"), key or env.get("key")  # what the sender says about the body
            if trusted:  # only from inside core: an outsider's labels, parent and hops could steer an item or dodge a loop guard
                labels = {**{str(k): str(v) for k, v in dict(env.get("labels") or {}).items()}, **(labels or {})}
                parent, hops = env.get("parent"), max(0, int(env.get("hops") or 0))
                if env.get("rerun") is not None:  # not a new item: a stored one is sent on to a later stage, bypassing the ones between
                    return await self.rerun(parent, env["rerun"])
        subject = new_subject_id()
        for label, value in (
            labels or {}
        ).items():  # not ``key``: that is the item's own id
            await self.labels.set(subject, label, value, actor)
        payload = IntakePayload(
            subject_id=subject,
            source=source,
            body=body,
            labels=labels or {},
            key=key,
            format=format,
            parent=parent,
            hops=hops,
        )
        await self.jobs.enqueue(
            FORMAT_KIND, payload, subject=subject, queue=DEFAULT, actor=actor
        )
        return subject

    async def rerun(self, subject: Any, spec: Any) -> SubjectId:
        """The one door for a rerun: the ``item_rerun`` operation calls it directly, and a trusted envelope reaches it through ``submit``.
        ``rerun`` = ``{"stage": "enrich"|"act", "routes": [...], "source": "..."}`` on a trusted envelope whose ``parent`` is the
        stored message. Intake and triage are skipped: the message is rebuilt from what the ingestor kept and handed to that stage.
        shortcut: runs every enricher of the stage (no ``plugin`` or ``fresh`` yet), and act needs its routes named, since a stored
        message does not keep what its triagers asked for."""
        stage = spec.get("stage") if isinstance(spec, dict) else None
        if stage not in RERUN_STAGES:
            raise InvalidInput(f"rerun.stage must be one of {', '.join(RERUN_STAGES)}")
        if not isinstance(subject, str) or not subject:
            raise InvalidInput("a rerun names the stored message in the envelope's parent")
        routes = [str(r) for r in spec.get("routes") or []]
        if stage == "act" and not routes:
            raise InvalidInput("rerun.routes: name the routes an act rerun answers")
        if self.rerun_hook is None:
            raise Unavailable("nothing can rebuild a stored item yet")
        await self.rerun_hook(subject, str(spec.get("source") or "rerun"), stage, routes)
        return SubjectId(subject)

    def _order(self, stage: str) -> list[Triager]:
        found = self.triager_source(stage) if self.triager_source else self.triagers
        return sorted(found, key=lambda t: (t.rank, t.name))

    def _stages(self) -> list[str]:
        """The triager stages, in the order they were made: an item goes through them one after another."""
        return [q.name for q in self.jobs.queues.all() if q.kind == "triager"]

    async def _format_job(self, p: IntakePayload, job: Job) -> None:
        """The first step, in ``default``: split the raw item into parts (in memory, nothing stored), then
        hand it to the first triager stage (``default`` itself when there is none)."""
        fmt = p.format or (
            "json" if isinstance(p.body, dict) else None
        )  # JSON is the plugins' common language
        formatter = self.formatter_for(fmt) if (self.formatter_for and fmt) else None
        parts, outcome, why = whole(p.body), "none", ""
        if (
            p.format and formatter is None
        ):  # only a format someone asked for can go unmatched: an implicit json that nobody handles is just "none"
            outcome, why = "unmatched", f"no formatter for {fmt!r}"
        elif formatter is not None:
            try:
                made = await formatter.split(p.body)
                parts, outcome = (
                    {n: Part.model_validate(v) for n, v in dict(made).items()}
                    or whole(p.body)
                ), "ok"
            except Exception as exc:  # a body that will not split is still an item: whole, or held, as configured
                outcome, why = (
                    "failed",
                    f"{fmt} formatter could not parse it ({type(exc).__name__})",
                )
        await self.labels.set(p.subject_id, "format", outcome, SYSTEM)
        if outcome in ("unmatched", "failed"):
            log.warning(
                "item not formatted", job_id=job.id, format=fmt, outcome=outcome
            )
            if (
                getattr(self.jobs.settings, f"{outcome}_format") == "fail"
            ):  # stays in Intake's failed jobs; requeue it once there is a formatter
                raise PermanentJobError(why)
        await self.labels.set(
            p.subject_id, "triage", "pending", SYSTEM
        )  # "triage me": a stage that decides clears it
        stages = self._stages()
        chosen = await self.labels.get(
            p.subject_id, "queue"
        )  # a provider may pick the entry stage at submit
        entry = chosen if chosen in stages else (stages[0] if stages else DEFAULT)
        if (
            chosen == entry
        ):  # spent: it named this stage, and the next stage's label is the triager's to write
            await self.labels.clear_machine(p.subject_id, SYSTEM, "queue")
        await self.jobs.enqueue(
            KIND,
            p.model_copy(update={"parts": parts}),
            subject=p.subject_id,
            queue=entry,
            actor=SYSTEM,
        )

    async def _triage_job(self, p: IntakePayload, job: Job) -> IntakeResult:
        item = IntakeItem(p.subject_id, p.source, p.body, p.labels, p.key, p.parts)
        who, verdict, said = SYSTEM, None, []
        try:
            for t in self._order(
                job.queue
            ):  # the logicians' bar: any no ends it, and the last to answer decides
                v = await t.triage(
                    replace(item, verdicts={a.label: r for a, r in said})
                )
                if v is None:
                    continue
                who, verdict = Actor("system", t.name), v
                said.append((who, v))
                if v.kind == "reject":
                    break
        except Exception:
            if not (
                self.jobs.settings.open_gate_on_final_failure
                and job.attempts >= job.max_attempts
            ):
                raise  # intake backs up; requeue replays it
            log.warning(
                "triager failed on the last attempt; treating as unsure", job_id=job.id
            )
            verdict = Verdict("unsure", "triage failed")
            said.append((who, verdict))
        if verdict is None:
            stages = self._stages()
            if job.queue in stages and stages.index(job.queue) + 1 < len(
                stages
            ):  # nobody here decided: the next stage may
                await self.jobs.enqueue(
                    KIND,
                    p,
                    subject=p.subject_id,
                    queue=stages[stages.index(job.queue) + 1],
                    actor=SYSTEM,
                )
                return IntakeResult(verdict="passed")
            verdict = Verdict("unsure", "no triager decided")
            said.append((who, verdict))
        try:
            for (
                actor,
                r,
            ) in (
                said
            ):  # every triager that answered labels the item, under its own name
                for key, value in r.labels.items():
                    await self.labels.set(p.subject_id, key, value, actor)
        except LabelError as exc:  # a triager bug: a retry would repeat it
            raise PermanentJobError(str(exc)) from exc
        await self.labels.clear_machine(
            p.subject_id, who, "triage"
        )  # decided: no longer waiting
        keep = await self._wants(
            "store",
            p.subject_id,
            verdict.kind != "reject" and self.jobs.settings.store_by_default,
        )
        if verdict.kind == "reject":
            if verdict.learn and self.router is not None:
                await self._learn(item)
            if not keep:
                await self._reject(item, verdict, who)
                return IntakeResult(verdict="reject")
        elif not keep:  # passed, but not kept: it goes on under its provisional id
            return IntakeResult(verdict=verdict.kind, subject_id=p.subject_id)
        await self.jobs.enqueue(
            STORE_KIND, StorePayload(**p.model_dump(), verdict=verdict.kind, reason=verdict.reason, by=who.label or who.kind,
                                     routes=list(dict.fromkeys(r for _, a in said for r in a.route))),
            subject=p.subject_id, queue=STORE, actor=who,
        )  # fmt: skip
        return IntakeResult(verdict=verdict.kind)

    async def _store_job(self, p: StorePayload, job: Job) -> IntakeResult:
        """The ``store`` stage: ingest the item and move its labels to the real subject."""
        if self.ingest is None:
            raise Unavailable(
                "intake has no ingest hook; nothing can store this item yet"
            )
        real = await self.ingest(
            IntakeItem(p.subject_id, p.source, p.body, p.labels, p.key, p.parts)
        )
        await self.labels.move(p.subject_id, real)
        async with self.db.transaction() as tx:
            await self.changelog.append(
                tx, actor=Actor("system", p.by), subject_id=real, key="intake", op="ingest",
                new=json.dumps({"from": p.subject_id, "source": p.source, "verdict": p.verdict,
                                "reason": p.reason}),
            )  # fmt: skip
        if self.router is not None and await self._wants(
            "embed", real, self.jobs.settings.embed_by_default
        ):
            item = IntakeItem(p.subject_id, p.source, p.body, p.labels, p.key, p.parts)
            await self.jobs.enqueue(
                EMBED_KIND, EmbedPayload(subject_id=real, text=item_text(item), confidential=p.labels.get("confidential") == "true"),
                subject=real, queue=EMBED, actor=Actor("system", p.by),
            )  # fmt: skip
        if self.on_stored is not None:
            from oaken_core.stagerun import StagePayload

            await self.on_stored(StagePayload(**{**p.model_dump(include=set(StagePayload.model_fields)), "subject_id": real}))
        return IntakeResult(verdict=p.verdict, subject_id=real)

    async def _embed_job(self, p: EmbedPayload, job: Job) -> None:
        """The ``embed`` stage: file the item's vector under its subject. Waits while no embedder or
        vector store is healthy (the router defers)."""
        await self.router.remember(
            ITEMS, p.subject_id, p.text, confidential=p.confidential
        )

    async def _wants(self, label: str, subject_id: str, default: bool) -> bool:
        """The item's ``label`` (true/false) if it has one, else ``default``."""
        value = await self.labels.get(subject_id, label)
        return default if value is None else value == "true"

    async def _learn(self, item: IntakeItem) -> None:
        """Keep the junk's vector and hash. A failure backs intake up (the job retries) rather than
        drop the lesson. No-op while ``[router] junk_index`` is off."""
        await self.router.learn_junk(
            item_text(item), confidential=item.labels.get("confidential") == "true"
        )

    async def _reject(self, item: IntakeItem, verdict: Verdict, who: Actor) -> None:
        """Drop the item. The log row holds no content: only a hash, so a mistake can be
        recognised later without keeping the text."""
        digest = hashlib.sha256(
            json.dumps(item.body, sort_keys=True, default=str).encode()
        ).hexdigest()
        async with self.db.transaction() as tx:
            await self.changelog.append(
                tx, actor=who, subject_id=item.subject_id, key="intake", op="reject",
                new=json.dumps({"source": item.source, "reason": verdict.reason, "sha256": digest}),
            )  # fmt: skip
        await self.labels.clear_machine(item.subject_id, who)
        await self.labels.clear_human_all(item.subject_id, who)
