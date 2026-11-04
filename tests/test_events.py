# SPDX-License-Identifier: Apache-2.0
from dataclasses import dataclass

import pytest

from oaken_core.clock import FakeClock
from oaken_core.events import Event, EventBus, JobStateChanged, PluginEvent, TraceEvent, event_data
from oaken_core.ids import JobId


def job_event(clock: FakeClock, status: str = "queued") -> JobStateChanged:
    return JobStateChanged(ts=clock.now(), job_id=JobId("j"), kind="k", queue="default", status=status)


@dataclass(frozen=True, kw_only=True)
class Custom(PluginEvent):
    n: int = 0


async def test_a_handler_gets_matching_events_only(clock: FakeClock) -> None:
    bus, seen = EventBus(), []

    async def on(e: Event) -> None:
        seen.append(e)

    bus.subscribe(JobStateChanged, on)
    await bus.publish(job_event(clock))
    await bus.publish(Custom(ts=clock.now()))
    assert [type(e) for e in seen] == [JobStateChanged]


async def test_a_failing_handler_hurts_nobody(clock: FakeClock) -> None:
    bus, seen = EventBus(), []

    async def bad(e: Event) -> None:
        raise RuntimeError("boom")

    async def good(e: Event) -> None:
        seen.append(e)

    bus.subscribe(Event, bad)
    bus.subscribe(Event, good)
    await bus.publish(job_event(clock))
    assert len(seen) == 1


async def test_a_slow_stream_drops_the_oldest_and_counts_it(clock: FakeClock) -> None:
    bus = EventBus()
    stream = bus.stream(maxsize=2)
    for status in ("a", "b", "c"):
        await bus.publish(job_event(clock, status))
    got = [(await stream.__anext__()).status, (await stream.__anext__()).status]  # type: ignore[attr-defined]
    assert got == ["b", "c"] and stream.dropped == 1
    await stream.aclose()


async def test_trace_events_reach_only_streams_that_ask_for_them(clock: FakeClock) -> None:
    bus, seen = EventBus(), []

    async def persist(e: Event) -> None:
        seen.append(e)

    bus.subscribe(Event, persist)
    plain, tracing = bus.stream(), bus.stream(TraceEvent)
    bus.publish_nowait(TraceEvent(ts=clock.now(), kind="llm.call", data={"text": "private"}))
    assert seen == []  # a persisting handler never sees private text
    assert (await tracing.__anext__()).kind == "llm.call"  # type: ignore[attr-defined]
    await bus.publish(job_event(clock))
    assert isinstance(await plain.__anext__(), JobStateChanged)  # the plain stream skipped the trace
    await plain.aclose()
    await tracing.aclose()


def test_event_data_is_plain_json(clock: FakeClock) -> None:
    data = event_data(job_event(clock))
    assert data["queue"] == "default" and isinstance(data["ts"], str)
