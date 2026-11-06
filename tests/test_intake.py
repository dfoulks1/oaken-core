# SPDX-License-Identifier: Apache-2.0
import json

import pytest

from oaken_core.actors import USER
from oaken_core.ids import SubjectId
from oaken_core.intake import KIND, Intake, IntakeItem, Verdict
from oaken_core.jobs import Selector
from oaken_core.settings import JobsSettings

from .conftest import Kit
from .test_jobs import Ping

SECRET = "the quick brown fox"


class Rules:
    name, rank = "rules", 10

    def __init__(self, verdict: Verdict | None) -> None:
        self.verdict, self.calls = verdict, 0

    async def triage(self, item: IntakeItem) -> Verdict | None:
        self.calls += 1
        return self.verdict


class Model(Rules):
    name, rank = "model", 90


class Broken(Rules):
    name, rank = "broken", 10

    async def triage(self, item: IntakeItem) -> Verdict | None:
        raise RuntimeError("model is down")


def make(kit: Kit, triagers: list, stored: list | None = None, ingest: bool = True) -> Intake:  # type: ignore[type-arg]
    stored = stored if stored is not None else []

    async def do_ingest(item: IntakeItem) -> SubjectId:
        stored.append(item)
        return SubjectId(f"real-{len(stored)}")

    intake = Intake(kit.jobs, kit.labels, kit.db, kit.log, triagers=triagers,
                    ingest=do_ingest if ingest else None)  # fmt: skip
    intake.register(kit.runner)
    return intake


async def test_a_reject_stores_nothing_and_logs_one_content_free_row(kit: Kit) -> None:
    stored: list = []  # type: ignore[type-arg]
    intake = make(kit, [Rules(Verdict("reject", "junk", {"priority": "low"}))], stored)
    prov = await intake.submit("web", {"text": SECRET}, labels={"confidential": "true"})
    await kit.runner.drain()
    assert stored == []
    assert await kit.labels.all(prov) == {}  # the provisional labels are gone too
    (job,) = await kit.jobs.query(kind=KIND)
    assert job.status == "succeeded" and job.payload_json == "{}" and SECRET not in (job.result_json or "")
    rows = [r for r in await kit.log.rows(kit.db, prov) if r.op == "reject"]
    assert len(rows) == 1
    meta = json.loads(rows[0].new_value or "")
    assert meta["reason"] == "junk" and len(meta["sha256"]) == 64
    assert SECRET not in json.dumps([r.__dict__ for r in await kit.log.rows(kit.db)])


async def test_an_accept_ingests_and_the_labels_follow_the_item(kit: Kit) -> None:
    kit.queues.new_queue("enrichers")
    stored: list = []  # type: ignore[type-arg]
    intake = make(kit, [Rules(Verdict("accept", "fit", {"queue": "enrichers", "priority": "high"}))], stored)
    prov = await intake.submit("web", {"text": SECRET}, labels={"confidential": "true"})
    await kit.runner.drain()
    assert [i.body for i in stored] == [{"text": SECRET}]
    assert await kit.labels.all(prov) == {}
    assert await kit.labels.all("real-1") == {"queue": "enrichers", "priority": "high", "confidential": "true"}
    nxt = await kit.jobs.enqueue("later", Ping(), subject="real-1")
    assert (await kit.jobs.get(nxt.job_id)).queue == "enrichers"  # type: ignore[union-attr]
    assert any(r.op == "ingest" for r in await kit.log.rows(kit.db, "real-1"))
    (job,) = await kit.jobs.query(kind=KIND)
    assert job.payload_json == "{}"  # the data lives in the store now


async def test_the_lowest_rank_decides_and_later_triagers_are_not_asked(kit: Kit) -> None:
    rules, model = Rules(Verdict("accept")), Model(Verdict("reject"))
    intake = make(kit, [model, rules])
    await intake.submit("web", "x")
    await kit.runner.drain()
    assert (rules.calls, model.calls) == (1, 0)


async def test_a_pass_goes_to_the_next_triager_and_no_verdict_means_unsure(kit: Kit) -> None:
    rules, model = Rules(None), Model(Verdict("unsure", "ask a human"))
    stored: list = []  # type: ignore[type-arg]
    intake = make(kit, [rules, model], stored)
    await intake.submit("web", "x")
    await kit.runner.drain()
    assert (rules.calls, model.calls, len(stored)) == (1, 1, 1)


async def test_no_verdict_at_all_means_unsure_and_the_item_is_ingested(kit: Kit) -> None:
    stored: list = []  # type: ignore[type-arg]
    intake = make(kit, [Rules(None)], stored)
    await intake.submit("web", "x")
    await kit.runner.drain()
    assert len(stored) == 1


async def test_a_broken_triager_backs_up_intake_and_loses_nothing(kit: Kit) -> None:
    stored: list = []  # type: ignore[type-arg]
    intake = make(kit, [Broken(None)], stored)
    await intake.submit("web", {"text": SECRET})
    for _ in range(4):
        await kit.runner.drain()
        kit.clock.advance(seconds=1000)
    (job,) = await kit.jobs.query(kind=KIND)
    assert job.status == "failed" and SECRET in job.payload_json  # payload kept, never purged
    assert stored == []
    (default,) = await kit.jobs.stats()
    assert default.failed == 1 and "model is down" in (default.last_error or "")


async def test_requeue_replays_failed_intake_once_the_triager_is_fixed(kit: Kit) -> None:
    stored: list = []  # type: ignore[type-arg]
    triager = Broken(None)
    intake = make(kit, [triager], stored)
    await intake.submit("web", "x")
    for _ in range(4):
        await kit.runner.drain()
        kit.clock.advance(seconds=1000)
    intake.triagers[:] = [Rules(Verdict("accept"))]  # the fix
    res = await kit.jobs.requeue(Selector(status="failed", kind=KIND), USER, retriage=True)
    assert len(res.requeued) == 1
    await kit.runner.drain()
    assert len(stored) == 1


async def test_open_gate_treats_the_last_failure_as_unsure(kit: Kit) -> None:
    kit.jobs.settings = JobsSettings(backoff_jitter=0, backoff_base_seconds=1, open_gate_on_final_failure=True)
    stored: list = []  # type: ignore[type-arg]
    intake = make(kit, [Broken(None)], stored)
    await intake.submit("web", "x")
    for _ in range(4):
        await kit.runner.drain()
        kit.clock.advance(seconds=1000)
    assert len(stored) == 1  # ingested on the final attempt, not stuck


async def test_a_triager_naming_a_missing_queue_fails_for_good(kit: Kit) -> None:
    intake = make(kit, [Rules(Verdict("accept", "", {"queue": "nope"}))])
    await intake.submit("web", "x")
    await kit.runner.drain()
    (job,) = await kit.jobs.query(kind=KIND)
    assert job.status == "failed" and job.attempts == 1 and "no queue named" in (job.last_error or "")


async def test_with_no_ingest_hook_the_item_waits(kit: Kit) -> None:
    intake = make(kit, [Rules(Verdict("accept"))], ingest=False)
    await intake.submit("web", {"text": SECRET})
    await kit.runner.drain()
    (job,) = await kit.jobs.query(kind=KIND)
    assert job.status == "queued" and SECRET in job.payload_json
