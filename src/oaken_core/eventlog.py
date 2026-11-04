# SPDX-License-Identifier: Apache-2.0
"""A persisted record of the events core publishes: one JSON object per line.

The event bus is in memory, and the SSE ring behind ``/v1/events`` keeps the last thousand for a
client that reconnects. Neither survives a restart. ``EventLog`` is one more handler on the bus that
appends every event to ``events.jsonl`` (owner-only, rotated by size like the log file), so a person
can ask what happened before the page was opened, or before core was restarted.

A line is ``{"ts", "kind", "data"}``: the event's class name and its fields (``event_data``), which
are ids, states and numbers, never private text. ``tail`` reads the newest lines back from the end of
the file, and from its rotated copies if it needs more, without loading whole files.
"""

import json
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from oaken_core import paths
from oaken_core.events import Event, EventBus, event_data
from oaken_core.logging import get_logger

log = get_logger("oaken_core.eventlog")

BLOCK = 64 * 1024  # how much ``tail`` reads at a time, from the end


def event_log_path(name: str) -> Path:
    """Where ``[log] events_file`` points: relative names sit under the data directory."""
    p = Path(name).expanduser()
    return p if p.is_absolute() else paths.data_dir() / p


class EventLog:
    def __init__(self, path: Path, *, max_bytes: int, backups: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self._fh: TextIO | None = None
        self._failed = False  # one warning for a disk that will not take writes, not one per event

    # -- writing ----------------------------------------------------------------------------
    def attach(self, bus: EventBus) -> Callable[[], None]:
        async def on_event(event: Event) -> None:
            self.append(event)

        return bus.subscribe(Event, on_event)

    def _open(self) -> TextIO:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")

    def append(self, event: Event) -> None:
        self.append_line(event.ts.isoformat(), type(event).__name__, event_data(event))

    def append_line(self, ts: str, kind: str, data: dict[str, Any]) -> None:
        line = json.dumps(
            {"ts": ts, "kind": kind, "data": data}, default=str, separators=(",", ":"), ensure_ascii=False
        ) + "\n"
        try:
            if self._fh is None:
                self._fh = self._open()
            if self._fh.tell() + len(line) > self.max_bytes:
                self._rotate()
            self._fh.write(line)
            self._fh.flush()
            self._failed = False
        except OSError as exc:  # losing history must never stop the event from being delivered
            if not self._failed:
                log.warning("event log not written", path=str(self.path), error=str(exc))
            self._failed = True

    def _rotate(self) -> None:
        if self._fh is not None:
            self._fh.close()
        for i in range(self.backups, 0, -1):
            src = self.path if i == 1 else self._backup(i - 1)
            if src.exists():
                src.replace(self._backup(i))
        if not self.backups:
            self.path.unlink(missing_ok=True)
        self._fh = self._open()

    def _backup(self, n: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{n}")

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    # -- reading ----------------------------------------------------------------------------
    def _files(self) -> list[Path]:
        """Newest first: the live file, then .1, .2 ..."""
        found = [self.path] if self.path.exists() else []
        found += [p for i in range(1, self.backups + 1) if (p := self._backup(i)).exists()]
        return found

    @staticmethod
    def _lines_from_end(path: Path) -> Any:
        """The file's lines, last first, reading ``BLOCK`` bytes at a time from the end."""
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            tail = b""
            while position > 0:
                step = min(BLOCK, position)
                position -= step
                handle.seek(position)
                chunk = handle.read(step) + tail
                *rest, tail = chunk.split(b"\n")  # ``tail`` may be a partial first line
                for raw in reversed(rest):
                    if raw:
                        yield raw
            if tail:
                yield tail

    def tail(
        self,
        limit: int = 100,
        *,
        since: datetime | None = None,
        kinds: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """The newest ``limit`` events (oldest first among them) that are not before ``since`` and
        are of one of ``kinds``. A line that is not valid JSON (a write cut off by a crash) is
        skipped."""
        found: list[dict[str, Any]] = []
        for path in self._files():
            try:
                for raw in self._lines_from_end(path):
                    try:
                        item = json.loads(raw)
                        ts = datetime.fromisoformat(item["ts"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    if since is not None and ts < since:
                        return list(reversed(found))  # older lines only from here on
                    if kinds is not None and item.get("kind") not in kinds:
                        continue
                    found.append(item)
                    if len(found) >= limit:
                        return list(reversed(found))
            except OSError:
                continue
        return list(reversed(found))
