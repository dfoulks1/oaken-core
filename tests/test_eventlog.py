# SPDX-License-Identifier: Apache-2.0
"""The event log: one JSON line per event, owner-only, rotated, read back from the end."""

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oaken_core.eventlog import EventLog
from oaken_core.events import Event, EventBus

T0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True, kw_only=True)
class Ping(Event):
    n: int = 0


@dataclass(frozen=True, kw_only=True)
class Pong(Event):
    n: int = 0


def ping(n: int, kind: type[Event] = Ping) -> Event:
    return kind(ts=T0 + timedelta(seconds=n), n=n)  # type: ignore[call-arg]


def make(tmp_path: Path, **kw: int) -> EventLog:
    return EventLog(
        tmp_path / "events.jsonl",
        max_bytes=kw.get("max_bytes", 10_000),
        backups=kw.get("backups", 2),
    )


def test_every_event_is_a_line_with_its_kind_and_fields(tmp_path: Path) -> None:
    elog = make(tmp_path)
    elog.append(ping(1))
    elog.close()
    [line] = (tmp_path / "events.jsonl").read_text().splitlines()
    row = json.loads(line)
    assert row["kind"] == "Ping" and row["data"]["n"] == 1 and row["ts"].startswith("2026-10-24")
    assert "event_id" in row["data"]


def test_the_file_is_owner_only(tmp_path: Path) -> None:
    elog = make(tmp_path)
    elog.append(ping(1))
    assert (tmp_path / "events.jsonl").stat().st_mode & 0o777 == 0o600


def test_tail_returns_the_newest_in_order_and_honours_the_limit(tmp_path: Path) -> None:
    elog = make(tmp_path)
    for n in range(10):
        elog.append(ping(n))
    assert [e["data"]["n"] for e in elog.tail(3)] == [7, 8, 9]
    assert len(elog.tail(100)) == 10 and make(tmp_path / "nowhere").tail() == []


def test_tail_filters_by_kind_and_since(tmp_path: Path) -> None:
    elog = make(tmp_path)
    for n in range(6):
        elog.append(ping(n, Ping if n % 2 else Pong))
    assert [e["data"]["n"] for e in elog.tail(10, kinds={"Pong"})] == [0, 2, 4]
    assert [e["data"]["n"] for e in elog.tail(10, since=T0 + timedelta(seconds=4))] == [4, 5]


def test_rotation_keeps_the_newest_and_tail_reads_across_files(tmp_path: Path) -> None:
    elog = make(tmp_path, max_bytes=1024, backups=2)
    for n in range(60):
        elog.append(ping(n))
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["events.jsonl", "events.jsonl.1", "events.jsonl.2"]
    got = [e["data"]["n"] for e in elog.tail(40)]
    live = len((tmp_path / "events.jsonl").read_text().splitlines())
    assert got == list(range(60 - len(got), 60))  # the newest, in order
    assert len(got) > live and got[-1] == 59  # it went on into the rotated files
    assert len(got) < 60  # and what rotated out past the last backup is gone


def test_a_line_cut_off_by_a_crash_is_skipped(tmp_path: Path) -> None:
    elog = make(tmp_path)
    elog.append(ping(1))
    elog.close()
    with (tmp_path / "events.jsonl").open("a") as f:
        f.write('{"ts": "2026-10-24T12:00:09+00:00", "kind": "Pi')
    assert [e["data"]["n"] for e in make(tmp_path).tail(10)] == [1]


def test_a_disk_that_will_not_take_writes_never_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    elog = EventLog(blocker / "events.jsonl", max_bytes=1000, backups=1)  # a parent that is a file
    elog.append(ping(1))
    elog.append(ping(2))  # still no raise, and one warning rather than one per event


async def test_attached_to_the_bus_it_records_what_is_published(tmp_path: Path) -> None:
    bus, elog = EventBus(), make(tmp_path)
    detach = elog.attach(bus)
    await bus.publish(ping(1))
    detach()
    await bus.publish(ping(2))  # after detaching, nothing more
    assert [e["data"]["n"] for e in elog.tail(10)] == [1]


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_a_long_file_is_read_from_the_end(tmp_path: Path) -> None:
    elog = make(tmp_path, max_bytes=50_000_000, backups=0)
    for n in range(3000):
        elog.append(ping(n))
    assert (tmp_path / "events.jsonl").stat().st_size > 64 * 1024  # more than one block
    assert [e["data"]["n"] for e in elog.tail(5)] == [2995, 2996, 2997, 2998, 2999]
    assert len(elog.tail(2500)) == 2500
