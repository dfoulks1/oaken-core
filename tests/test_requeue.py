# SPDX-License-Identifier: Apache-2.0
import pytest

from oaken_core.actors import SYSTEM, USER
from oaken_core.errors import InvalidInput
from oaken_core.jobs import Selector

from .conftest import Kit
from .test_jobs import Ping


async def fail_one(kit: Kit, kind: str = "k", **kw: object) -> str:
    enq = await kit.jobs.enqueue(kind, Ping(), max_attempts=1, **kw)  # type: ignore[arg-type]
    where = (await kit.jobs.get(enq.job_id)).queue  # type: ignore[union-attr]
    job = await kit.jobs.claim("w", where, [kind])
    assert job
    assert await kit.jobs.fail(job.id, "w", "broke") == "failed"
    return enq.job_id


async def test_a_failed_job_is_requeued_with_a_clean_slate_and_a_log_row(kit: Kit) -> None:
    jid = await fail_one(kit)
    res = await kit.jobs.requeue(Selector(job_id=jid), USER)
    assert res.requeued == (jid,)
    job = await kit.jobs.get(jid)
    assert job and (job.status, job.attempts, job.last_error) == ("queued", 0, None)
    (row,) = [r for r in await kit.log.rows(kit.db) if r.op == "requeue"]
    assert (row.actor_kind, row.old_value, row.new_value) == ("user", "failed@default", "queued@default")


async def test_a_selector_requeues_many_and_there_is_no_everything(kit: Kit) -> None:
    a, b = await fail_one(kit), await fail_one(kit)
    with pytest.raises(InvalidInput, match="selector"):
        await kit.jobs.requeue(Selector(), USER)
    res = await kit.jobs.requeue(Selector(status="failed", queue="default"), USER)
    assert set(res.requeued) == {a, b}


async def test_running_and_finished_jobs_are_left_alone(kit: Kit) -> None:
    await kit.jobs.enqueue("k", Ping())
    running = await kit.jobs.claim("w", "default", ["k"])
    assert running
    res = await kit.jobs.requeue(Selector(job_id=running.id), USER)
    assert res.requeued == ()
    with pytest.raises(InvalidInput):
        await kit.jobs.requeue(Selector(status="succeeded"), USER)


async def test_a_job_requeued_too_soon_is_skipped_then_allowed(kit: Kit) -> None:
    jid = await fail_one(kit)
    await kit.jobs.requeue(Selector(job_id=jid), USER)
    job = await kit.jobs.claim("w", "default", ["k"])
    assert job and await kit.jobs.fail(job.id, "w", "again") == "failed"
    soon = await kit.jobs.requeue(Selector(job_id=jid), USER)
    assert (soon.requeued, soon.cooling) == ((), (jid,))
    kit.clock.advance(seconds=61)
    assert (await kit.jobs.requeue(Selector(job_id=jid), USER)).requeued == (jid,)


async def test_to_moves_the_job_and_must_name_a_real_queue(kit: Kit) -> None:
    kit.queues.new_queue("review")
    jid = await fail_one(kit)
    await kit.jobs.requeue(Selector(job_id=jid), USER, to="review")
    job = await kit.jobs.get(jid)
    assert job and job.queue == "review"
    with pytest.raises(InvalidInput):
        await kit.jobs.requeue(Selector(job_id=jid), USER, to="nope")


async def test_retriage_clears_machine_labels_keeps_human_and_goes_to_default(kit: Kit) -> None:
    kit.queues.new_queue("enrichers")
    await kit.labels.set("s1", "priority", "high", USER)
    await kit.labels.set("s1", "priority", "low", SYSTEM)
    await kit.labels.set("s1", "queue", "enrichers", SYSTEM)
    jid = await fail_one(kit, "intake.triage", subject="s1")
    await kit.jobs.requeue(Selector(job_id=jid), USER, retriage=True)
    assert await kit.labels.all("s1") == {"priority": "high"}  # machine layer gone, human kept
    job = await kit.jobs.get(jid)
    assert job and job.queue == "default" and job.status == "queued"


async def test_retriage_is_for_intake_jobs_only(kit: Kit) -> None:
    jid = await fail_one(kit, "k")
    with pytest.raises(InvalidInput, match="intake"):
        await kit.jobs.requeue(Selector(job_id=jid), USER, retriage=True)
