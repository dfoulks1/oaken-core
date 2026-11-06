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
