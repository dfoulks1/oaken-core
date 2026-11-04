# SPDX-License-Identifier: Apache-2.0
from datetime import timedelta

import pytest
from pydantic import BaseModel

from oaken_core.actors import SYSTEM, USER
from oaken_core.errors import InvalidInput, InvalidResult
from oaken_core.jobs import PermanentJobError

from .conftest import Kit


class Ping(BaseModel):
    n: int = 0


class Pong(BaseModel):
    n: int


async def ok(p: Ping, job: object) -> Pong:
    return Pong(n=p.n + 1)


async def test_a_job_runs_and_succeeds(kit: Kit) -> None:
    kit.runner.register("k", Ping, ok, result_model=Pong)
    enq = await kit.queue.enqueue("k", Ping(n=1))
    assert await kit.runner.drain() == 1
    job = await kit.queue.get(enq.job_id)
    assert job and job.status == "succeeded" and job.result_json == '{"n":2}'


async def test_dedupe_returns_the_existing_job(kit: Kit) -> None:
    a = await kit.queue.enqueue("k", Ping(), dedupe_key="x")
    b = await kit.queue.enqueue("k", Ping(), dedupe_key="x")
    assert (a.created, b.created, a.job_id) == (True, False, b.job_id)


async def test_highest_priority_goes_first(kit: Kit) -> None:
    low = await kit.queue.enqueue("k", Ping(), priority=10)
    high = await kit.queue.enqueue("k", Ping(), priority=90)
    job = await kit.queue.claim("w", "default", ["k"])
    assert job and job.id == high.job_id and job.id != low.job_id


async def test_routing_by_label_then_name_then_default(kit: Kit) -> None:
    kit.queues.new_queue("enrichers")
    await kit.labels.set("s1", "queue", "enrichers", SYSTEM)
    by_label = await kit.queue.enqueue("k", Ping(), subject="s1")
    plain = await kit.queue.enqueue("k", Ping(), subject="s-unlabeled")
    named = await kit.queue.enqueue("k", Ping(), subject="s1", queue="default")  # a name wins
    got = {j: (await kit.queue.get(j)).queue for j in (by_label.job_id, plain.job_id, named.job_id)}  # type: ignore[union-attr]
    assert list(got.values()) == ["enrichers", "default", "default"]


async def test_a_human_relabel_decides_the_queue_over_a_machine_one(kit: Kit) -> None:
    kit.queues.new_queue("enrichers")
    kit.queues.new_queue("review")
    await kit.labels.set("s1", "queue", "enrichers", SYSTEM)
    await kit.labels.set("s1", "queue", "review", USER)
    await kit.labels.set("s1", "queue", "enrichers", SYSTEM)  # a re-run does not move it
    job = await kit.queue.get((await kit.queue.enqueue("k", Ping(), subject="s1")).job_id)
    assert job and job.queue == "review"


async def test_an_unknown_queue_is_refused(kit: Kit) -> None:
    with pytest.raises(InvalidInput):
        await kit.queue.enqueue("k", Ping(), queue="nope")


async def test_a_worker_claims_only_its_own_queue(kit: Kit) -> None:
    kit.queues.new_queue("enrichers")
    await kit.queue.enqueue("k", Ping(), queue="enrichers")
    assert await kit.queue.claim("w", "default", ["k"]) is None
    assert await kit.queue.claim("w", "enrichers", ["k"]) is not None


async def test_a_paused_queue_yields_nothing_until_resumed(kit: Kit) -> None:
    await kit.queue.enqueue("k", Ping())
    await kit.queue.pause("default")
    assert await kit.queue.claim("w", "default", ["k"]) is None
    assert (await kit.queue.stats())[0].paused is True
    await kit.queue.resume("default")
    assert await kit.queue.claim("w", "default", ["k"]) is not None


async def test_failures_retry_with_backoff_then_fail(kit: Kit) -> None:
    async def boom(p: Ping, job: object) -> None:
        raise RuntimeError("token=abc123 broke")

    kit.runner.register("k", Ping, boom)
    enq = await kit.queue.enqueue("k", Ping(), max_attempts=2)
    await kit.runner.drain()
    job = await kit.queue.get(enq.job_id)
    assert job and job.status == "queued" and job.attempts == 1
    assert job.run_after - kit.clock.now() == timedelta(seconds=10)
    assert await kit.runner.drain() == 0  # not yet due
    kit.clock.advance(seconds=11)
    await kit.runner.drain()
    job = await kit.queue.get(enq.job_id)
    assert job and job.status == "failed" and job.attempts == 2
    assert "abc123" not in (job.last_error or "") and "[REDACTED]" in (job.last_error or "")


async def test_a_permanent_error_fails_at_once(kit: Kit) -> None:
    async def nope(p: Ping, job: object) -> None:
        raise PermanentJobError("no point retrying")

    kit.runner.register("k", Ping, nope)
    enq = await kit.queue.enqueue("k", Ping())
    await kit.runner.drain()
    job = await kit.queue.get(enq.job_id)
    assert job and job.status == "failed" and job.attempts == 1


async def test_an_expired_lease_is_reclaimed(kit: Kit) -> None:
    await kit.queue.enqueue("k", Ping())
    first = await kit.queue.claim("dead", "default", ["k"])
    assert first and await kit.queue.claim("other", "default", ["k"]) is None
    kit.clock.advance(seconds=31)
    again = await kit.queue.claim("other", "default", ["k"])
    assert again and again.id == first.id and again.attempts == 2


async def test_a_kind_at_its_cap_is_skipped(kit: Kit) -> None:
    await kit.queue.enqueue("k", Ping())
    await kit.queue.enqueue("k", Ping())
    assert await kit.queue.claim("a", "default", ["k"], {"k": 1}) is not None
    assert await kit.queue.claim("b", "default", ["k"], {"k": 1}) is None


async def test_cancel_a_queued_job(kit: Kit) -> None:
    enq = await kit.queue.enqueue("k", Ping())
    assert await kit.queue.cancel(enq.job_id) == "cancelled"
    assert await kit.queue.claim("w", "default", ["k"]) is None


async def test_a_result_must_match_its_schema(kit: Kit) -> None:
    kit.runner.register("k", Ping, ok, result_model=Pong)
    await kit.queue.enqueue("k", Ping())
    job = await kit.queue.claim("w", "default", ["k"])
    assert job
    with pytest.raises(InvalidResult):
        await kit.queue.complete(job.id, "w", {"wrong": 1})
    assert await kit.queue.complete(job.id, "w", Pong(n=1)) is True


async def test_every_queue_is_drained(kit: Kit) -> None:
    kit.queues.new_queue("enrichers")
    kit.runner.register("k", Ping, ok, result_model=Pong)
    await kit.queue.enqueue("k", Ping(), queue="default")
    await kit.queue.enqueue("k", Ping(), queue="enrichers")
    assert await kit.runner.drain() == 2


async def test_stats_show_depth_age_and_the_last_error(kit: Kit) -> None:
    await kit.queue.enqueue("k", Ping())
    kit.clock.advance(seconds=120)
    (default,) = await kit.queue.stats()
    assert (default.queued, default.failed) == (1, 0)
    assert default.oldest_queued_seconds == 120 and default.last_error is None
