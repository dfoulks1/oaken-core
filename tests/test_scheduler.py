# SPDX-License-Identifier: Apache-2.0
from datetime import timedelta

from oaken_core.scheduler import Scheduler

from .conftest import Kit
from .test_jobs import Ping


async def test_a_due_schedule_enqueues_into_its_named_queue_once(kit: Kit) -> None:
    kit.queues.new_queue("maintenance")
    sched = Scheduler(kit.db, kit.jobs, kit.clock)
    await sched.add("tidy", "k", Ping(), every=timedelta(seconds=60), queue="maintenance")
    assert await sched.tick() == 0  # not yet due
    kit.clock.advance(seconds=61)
    assert await sched.tick() == 1
    assert await sched.tick() == 0  # the slot moved on in the same transaction
    (job,) = await kit.jobs.query(kind="k")
    assert job.queue == "maintenance"


async def test_missed_runs_collapse_into_one(kit: Kit) -> None:
    sched = Scheduler(kit.db, kit.jobs, kit.clock)
    await sched.add("tidy", "k", Ping(), every=timedelta(seconds=60))
    kit.clock.advance(seconds=600)  # ten periods missed
    assert await sched.tick() == 1
    assert len(await kit.jobs.query(kind="k")) == 1


async def test_a_schedule_that_cannot_fire_is_postponed_and_does_not_stop_the_others(kit: Kit) -> None:
    sched = Scheduler(kit.db, kit.jobs, kit.clock)
    await sched.add("bad", "k", Ping(), every=timedelta(seconds=60))
    await sched.add("good", "k", Ping(), every=timedelta(seconds=60))
    await kit.db.execute("UPDATE schedules SET spec_kind = 'cron', spec = 'not a cron' WHERE name = 'bad'")  # a hand-edited spec
    kit.clock.advance(seconds=61)
    assert await sched.tick() == 1  # "good" fired, "bad" did not raise out of tick
    assert len(await kit.jobs.query(kind="k")) == 1
    assert await sched.next_run("bad") > kit.clock.now() + timedelta(seconds=30)  # postponed, not spinning
