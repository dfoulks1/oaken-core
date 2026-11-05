# SPDX-License-Identifier: Apache-2.0
"""Housekeeping: storage figures, vacuum as a job, the answer cache, the event log."""

from typing import Any

import pytest

from oaken_core.errors import Unavailable
from oaken_core.eventlog import EventLog
from oaken_core.events import JobStateChanged
from oaken_core.maintenance import VACUUM_KIND, register_maintenance_jobs
from oaken_core.operations.caller import CallerContext
from oaken_core.router.store import AnswerCache, CacheEntry

from .conftest import Ops

CLI = CallerContext.cli()


async def test_storage_info_reports_the_database_and_the_files_beside_it(ops: Ops) -> None:
    out = (await ops.client.call("storage_info", {}, CLI)).output
    assert out.core_db.endswith("core.db") and out.core_db_bytes > 0  # type: ignore[union-attr]


async def test_vacuum_is_a_proposal_then_one_job_that_shrinks_nothing_it_should_not(ops: Ops) -> None:
    register_maintenance_jobs(ops.kit.runner, ops.kit.db)
    asked = await ops.client.call("storage_vacuum", {}, CLI)
    assert asked.proposal is not None and not await ops.kit.queue.query(kind=VACUUM_KIND)
    first = (await ops.client.call("storage_vacuum", {}, CLI, confirm=True)).output
    again = (await ops.client.call("storage_vacuum", {}, CLI, confirm=True)).output
    assert first.created and not again.created and again.job_id == first.job_id  # type: ignore[union-attr]  # one at a time
    assert await ops.kit.runner.drain() == 1
    job = (await ops.kit.queue.query(kind=VACUUM_KIND))[0]
    assert job.status == "succeeded"


async def test_the_answer_cache_is_counted_and_cleared_by_analyzer(ops: Ops) -> None:
    cache = AnswerCache(ops.kit.db, ops.kit.clock)
    for analyzer, key in (("summary", "a"), ("summary", "b"), ("triage", "c")):
        await cache.put(cache.key(analyzer, "1", key, "m"), CacheEntry("{}", 1, 1))
    stats = (await ops.client.call("cache_stats", {}, CLI)).output
    assert stats.entries == 3 and stats.by_analyzer == {"summary": 2, "triage": 1}  # type: ignore[union-attr]
    asked = await ops.client.call("cache_clear", {"analyzer": "triage"}, CLI)
    assert asked.proposal is not None and asked.proposal.total_changes == 1  # shows what would go
    done = (await ops.client.call("cache_clear", {"analyzer": "triage"}, CLI, confirm=True)).output
    assert done.deleted == 1  # type: ignore[union-attr]
    assert "triage" not in (await ops.client.call("cache_stats", {}, CLI)).output.by_analyzer  # type: ignore[union-attr]


async def test_events_tail_reads_the_log_and_says_when_it_is_off(ops: Ops, tmp_path: Any) -> None:
    with pytest.raises(Unavailable, match="events_file"):
        await ops.client.call("events_tail", {}, CLI)
    log = EventLog(tmp_path / "events.jsonl", max_bytes=1_000_000, backups=1)
    ops.svc.eventlog = log
    log.append(JobStateChanged(ts=ops.kit.clock.now(), job_id="j1", kind="k", queue="default", status="queued", subject=None))
    out = (await ops.client.call("events_tail", {"kinds": ["JobStateChanged"], "limit": 5}, CLI)).output
    assert [e.kind for e in out.events] == ["JobStateChanged"] and out.events[0].data["job_id"] == "j1"  # type: ignore[union-attr]
    log.close()


async def test_none_of_the_maintenance_operations_is_offered_over_mcp(ops: Ops) -> None:
    mcp = {o.name for o in await ops.client.visible(CallerContext.mcp("me"))}
    assert not mcp & {"storage_info", "storage_vacuum", "cache_stats", "cache_clear", "events_tail"}
