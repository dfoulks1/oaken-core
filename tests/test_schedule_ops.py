# SPDX-License-Identifier: Apache-2.0
import json

import pytest

from oaken_core.errors import Conflict, InvalidInput, NotFound
from oaken_core.operations.caller import CallerContext
from oaken_core.scheduler import Scheduler

from .conftest import Ops

CLI = CallerContext.cli()


async def call(ops: Ops, name: str, inp: dict, confirm: bool = True):  # type: ignore[no-untyped-def]
    return await ops.client.call(name, inp, CLI, confirm=confirm)


@pytest.fixture
async def sops(ops: Ops) -> Ops:
    ops.svc.scheduler = Scheduler(ops.kit.db, ops.kit.jobs, ops.kit.clock)
    return ops


async def test_new_list_get_edit_delete(sops: Ops) -> None:
    new = {"name": "tidy", "kind": "k", "payload": '{"a": 1}', "every_seconds": 60}
    prop = await call(sops, "schedule_new", new, confirm=False)
    assert prop.proposal and not await sops.svc.scheduler.row("tidy")  # a proposal changes nothing
    await call(sops, "schedule_new", new)
    got = (await call(sops, "schedule_get", {"name": "tidy"})).output
    assert (got.every_seconds, got.cron, json.loads(got.payload)) == (60.0, None, {"a": 1})
    assert [r.name for r in (await call(sops, "schedule_list", {})).output.schedules] == ["tidy"]
    await call(sops, "schedule_edit", {"name": "tidy", "cron": "0 6 * * *", "priority": 10})
    got = (await call(sops, "schedule_get", {"name": "tidy"})).output
    assert (got.every_seconds, got.cron, got.priority, got.kind) == (None, "0 6 * * *", 10, "k")  # untouched fields stay
    await call(sops, "schedule_delete", {"name": "tidy"})
    assert await sops.svc.scheduler.row("tidy") is None


async def test_refusals(sops: Ops) -> None:
    ok = {"name": "tidy", "kind": "k", "every_seconds": 60}
    await call(sops, "schedule_new", ok)
    with pytest.raises(Conflict):
        await call(sops, "schedule_new", ok)
    with pytest.raises(InvalidInput, match="exactly one"):
        await call(sops, "schedule_new", {"name": "x", "kind": "k"})
    with pytest.raises(InvalidInput, match="payload"):
        await call(sops, "schedule_new", {**ok, "name": "y", "payload": "[1]"})
    with pytest.raises(InvalidInput):
        await call(sops, "schedule_new", {"name": "z", "kind": "k", "cron": "not cron"})
    with pytest.raises(InvalidInput, match="nothing to change"):
        await call(sops, "schedule_edit", {"name": "tidy"})
    with pytest.raises(NotFound):
        await call(sops, "schedule_delete", {"name": "nope"})
    with pytest.raises(InvalidInput, match="no queue"):
        await call(sops, "schedule_new", {**ok, "name": "q", "queue": "nope"})


async def test_run_enqueues_now_without_moving_the_next_run(sops: Ops) -> None:
    await call(sops, "schedule_new", {"name": "tidy", "kind": "k", "payload": '{"a": 1}', "every_seconds": 3600})
    before = await sops.svc.scheduler.next_run("tidy")
    await call(sops, "schedule_run", {"name": "tidy"})
    (job,) = await sops.kit.jobs.query(kind="k")
    assert json.loads(job.payload_json) == {"a": 1} and await sops.svc.scheduler.next_run("tidy") == before
