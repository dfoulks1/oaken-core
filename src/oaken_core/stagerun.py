# SPDX-License-Identifier: Apache-2.0
"""The loops after storing: enrich, then act (contracts in ``stages.py``, ADR 0019).

When the ``store`` stage has ingested an item, ``begin`` starts it here:

1. **Enrich.** One ``intake.enrich`` job per enricher stage (a queue of kind ``enricher``), in the order the stages
   were made. Every healthy enricher in the stage runs, by name; each sees what the ones before it added. A result
   is kept in the store as a record in ``enrichments`` (``<subject>:<enricher>``) and its labels go on the subject
   under the enricher's name. No enricher stage: the step is skipped.
2. **Act.** For each route a triager set on its verdict, one ``intake.act`` job, queued in the stage of the first
   action that answers it. With nobody answering, the job fails with "no action answers route X" and shows in the
   job list; ``retry_job`` runs it once an action is installed.

A failing enricher or action backs the item up (retry with backoff, then failed with the payload intact), as a
failing triager does. Payloads are scrubbed when a job succeeds. Audit is the log line: subject, action, route and
outcome, never content.

shortcut: no approval step (ADR 0019 4a: ``ask`` or ``auto``), so every action runs as ``auto``; and no
de-duplication on (subject, action, route), so an action must be idempotent itself. Add both when a destructive
action ships.
"""

from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, Field

from oaken_core.actors import SYSTEM, Actor
from oaken_core.errors import Conflict
from oaken_core.jobs import PRIORITY_INGEST, PRIORITY_USER, Job, JobRunner, Jobs, PermanentJobError
from oaken_core.labels import LabelError, LabelStore
from oaken_core.logging import get_logger
from oaken_core.parts import Part
from oaken_core.queues import DEFAULT
from oaken_core.stages import ActionTarget, EnrichTarget, Enrichment

log = get_logger("oaken_core.stages")

ENRICH_KIND = "intake.enrich"
ACT_KIND = "intake.act"
ENRICHMENTS = "enrichments"  # the store collection an enricher's result is kept in


class StagePayload(BaseModel):
    subject_id: str  # the real one: the item is stored
    source: str
    body: Any = None
    labels: dict[str, str] = Field(default_factory=dict)
    key: str | None = None
    parts: dict[str, Part] = Field(default_factory=dict)
    routes: list[str] = Field(default_factory=list)  # what the triagers asked for
    by: str = "system"
    enrichments: dict[str, dict[str, Any]] = Field(default_factory=dict)  # name -> {data, labels}


class ActPayload(StagePayload):
    route: str


class StageResult(BaseModel):
    ran: list[str] = Field(default_factory=list)  # the plugins that ran, by name


def _target(p: StagePayload) -> EnrichTarget:
    return EnrichTarget(p.subject_id, p.source, p.body, p.labels, p.key, p.parts,
                        enrichments={n: Enrichment(**e) for n, e in p.enrichments.items()})  # fmt: skip


class StageRunner:
    """``enrichers_for(stage)`` is the healthy enrichers of a stage as ``(name, plugin)``, in the order they run;
    ``actions_for(route)`` the healthy actions answering a route as ``(name, plugin, stage)``; ``store()`` the
    active store plugin, or None."""

    def __init__(
        self,
        jobs: Jobs,
        labels: LabelStore,
        *,
        enrichers_for: Callable[[str], Sequence[tuple[str, Any]]],
        actions_for: Callable[[str], Sequence[tuple[str, Any, str | None]]],
        store: Callable[[], Any] = lambda: None,
    ) -> None:
        self.jobs, self.labels = jobs, labels
        self.enrichers_for, self.actions_for, self.store = enrichers_for, actions_for, store

    def register(self, runner: JobRunner) -> None:
        runner.register(ENRICH_KIND, StagePayload, self._enrich_job, result_model=StageResult, scrub_payload=True)
        runner.register(ACT_KIND, ActPayload, self._act_job, result_model=StageResult, scrub_payload=True)

    def _stages(self, kind: str) -> list[str]:
        return [q.name for q in self.jobs.queues.all() if q.kind == kind]

    async def begin(self, p: StagePayload) -> None:
        """An item was stored: enrich it, then act on its routes."""
        await self._onward(p, None)

    async def _onward(self, p: StagePayload, after: str | None, priority: int = PRIORITY_INGEST) -> None:
        """Queue the enricher stage after ``after`` (the first when None); with none left, the actions."""
        stages = self._stages("enricher")
        nxt = stages if after is None else stages[stages.index(after) + 1 :]
        if nxt:
            await self.jobs.enqueue(ENRICH_KIND, p, subject=p.subject_id, queue=nxt[0], actor=SYSTEM, priority=priority)
            return
        await self._acts(p, priority)

    async def _acts(self, p: StagePayload, priority: int = PRIORITY_INGEST) -> None:
        for route in dict.fromkeys(p.routes):  # each once, in the order asked
            found = self.actions_for(route)
            stage = next((s for _, _, s in found if s), DEFAULT)
            await self.jobs.enqueue(ACT_KIND, ActPayload(**p.model_dump(), route=route), subject=p.subject_id, queue=stage, actor=SYSTEM, priority=priority)

    async def rerun(self, p: StagePayload, stage: str, routes: list[str], priority: int = PRIORITY_INGEST) -> None:
        """Send a stored item to one stage, skipping the ones before it. Enrich runs the enrichers and stops (no routes, so no actions
        follow); act runs the named routes' actions only."""
        if stage == "enrich":
            await self._onward(p.model_copy(update={"routes": []}), None, priority)
        else:
            await self._acts(p.model_copy(update={"routes": routes}), priority)

    async def _keep(self, subject: str, name: str, e: Enrichment) -> None:
        store, rid, data = self.store(), f"{subject}:{name}", {"data": e.data, "labels": e.labels}
        if store is None:
            return
        try:
            await store.create(ENRICHMENTS, data, id=rid)
        except Conflict:  # a rerun: the newest answer replaces the old
            await store.update(ENRICHMENTS, rid, data)

    async def _enrich_job(self, p: StagePayload, job: Job) -> StageResult:
        ran: list[str] = []
        for name, plugin in self.enrichers_for(job.queue):
            e = await plugin.enrich(_target(p))
            ran.append(name)
            if e is None:
                continue
            await self._keep(p.subject_id, name, e)
            try:
                for k, v in e.labels.items():
                    await self.labels.set(p.subject_id, k, v, Actor("system", name))
            except LabelError as exc:  # an enricher bug: a retry would repeat it
                raise PermanentJobError(str(exc)) from exc
            p = p.model_copy(update={"enrichments": {**p.enrichments, name: {"data": e.data, "labels": e.labels}}})
        # a user's request keeps its priority down the later enrich stages; anything else resets to the default, as before
        await self._onward(p, job.queue, job.priority if job.priority >= PRIORITY_USER else PRIORITY_INGEST)
        return StageResult(ran=ran)

    async def _act_job(self, p: ActPayload, job: Job) -> StageResult:
        found = self.actions_for(p.route)
        if not found:
            raise PermanentJobError(f"no action answers route {p.route!r}")
        target = ActionTarget(p.subject_id, p.source, p.body, p.labels, p.key, p.parts,
                              enrichments={n: Enrichment(**e) for n, e in p.enrichments.items()}, route=p.route)  # fmt: skip
        ran: list[str] = []
        for name, plugin, _ in found:
            result = await plugin.act(target)
            ran.append(name)
            log.info("action ran", action=name, route=p.route, subject_id=p.subject_id, ok=result.ok, detail=result.detail)
            if not result.ok:  # the action's own verdict: not a hiccup a retry would fix
                raise PermanentJobError(f"{name}: {result.detail or 'failed'}")
        return StageResult(ran=ran)
