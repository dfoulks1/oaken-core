# SPDX-License-Identifier: Apache-2.0
"""The log, kept, and on the event bus. Every line core logs is published as a ``LogEvent`` (streams only, like the trace). With
no ``[log] file`` a stream consumer writes them to the ``log_lines`` table; with one, the file is the log. Logging is on unless
``[log] enabled = false``.

Lines are written in a batch every half second by one background task, so a log call never waits on the database and a database
that will not take a line never costs a job. A line that cannot be written is dropped.

shortcut: one table in the core database, pruned by age (``[log] keep_days``); a very chatty install wants the file.
"""

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from oaken_core.db import Database
from oaken_core.events import EventBus, LogEvent
from oaken_core.logging import ROOT, _event

INSERT = "INSERT INTO log_lines (ts, level, logger, plugin, queue, event, data) VALUES (?,?,?,?,?,?,?)"
PLUGIN_LOGGER = ROOT + ".plugin."
MAX_BUFFER = 10_000  # a stuck database must not eat memory: the oldest half goes
SHAPE = ("ts", "level", "logger", "event")


def plugin_of(logger: str) -> str:
    """``oaken_core.plugin.triager.llm`` as ``triager/llm``; "" for any other logger."""
    if not logger.startswith(PLUGIN_LOGGER):
        return ""
    kind, _, name = logger[len(PLUGIN_LOGGER):].partition(".")
    return f"{kind}/{name}" if name else ""


class LogToBus(logging.Handler):
    """Publishes each record as a ``LogEvent``. Never raises, never waits."""

    def __init__(self, bus: EventBus) -> None:
        super().__init__()
        self.bus = bus

    def emit(self, record: logging.LogRecord) -> None:
        try:
            e = _event(record)
            rest = {k: v for k, v in e.items() if k not in SHAPE}
            self.bus.publish_nowait(LogEvent(ts=datetime.fromisoformat(e["ts"]), level=e["level"], logger=e["logger"], event=e["event"],
                                             plugin=plugin_of(e["logger"]), queue=str(rest.get("queue", "")), data=rest))  # fmt: skip
        except Exception:  # noqa: BLE001, S110 - a log call never costs the caller
            pass

    def attach(self) -> None:
        logging.getLogger(ROOT).addHandler(self)

    def detach(self) -> None:
        logging.getLogger(ROOT).removeHandler(self)


class LogTable:
    """Consumes ``LogEvent``s from the bus and writes them to ``log_lines``, once a database is bound."""

    def __init__(self, bus: EventBus, keep_days: int) -> None:
        self.stream = bus.stream(LogEvent, maxsize=MAX_BUFFER)  # taken now, so lines from startup are not lost
        self.keep_days, self.db = keep_days, None
        self.buffer: list[tuple[str, ...]] = []
        self.tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        self.tasks = [asyncio.create_task(self._consume(), name="oaken-log-in"), asyncio.create_task(self._flush_loop(), name="oaken-log-out")]

    def bind(self, db: Database) -> None:
        self.db = db

    async def _consume(self) -> None:
        async for ev in self.stream:
            if isinstance(ev, LogEvent):
                ts = ev.ts.isoformat(timespec="milliseconds")
                self.buffer.append((ts, ev.level, ev.logger, ev.plugin, ev.queue, ev.event, json.dumps(ev.data, default=str)))
                if len(self.buffer) > MAX_BUFFER:
                    del self.buffer[: MAX_BUFFER // 2]

    async def flush(self) -> None:
        if self.db is None or not self.buffer:
            return
        rows, self.buffer = self.buffer, []
        with contextlib.suppress(Exception):
            await self.db.executemany(INSERT, rows)

    async def _flush_loop(self) -> None:
        ticks = 0
        while True:
            await asyncio.sleep(0.5)
            await self.flush()
            ticks += 1
            if ticks % 7200 == 1 and self.db is not None:  # at the start, then every hour
                cutoff = (datetime.now(UTC) - timedelta(days=self.keep_days)).isoformat(timespec="milliseconds")
                with contextlib.suppress(Exception):
                    await self.db.execute("DELETE FROM log_lines WHERE ts < ?", (cutoff,))

    async def stop(self) -> None:
        await self.stream.aclose()  # the consumer drains what is left, then ends
        for task in self.tasks:
            if task is self.tasks[1]:
                task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.gather(*self.tasks)
        await self.flush()


def read_file(path: Path, limit: int) -> list[dict[str, object]]:
    """The last ``limit`` lines of the JSON log file (and its newest backup, if the file is short), parsed."""
    lines: list[str] = []
    for p in (path.with_name(path.name + ".1"), path):
        with contextlib.suppress(OSError):
            lines += p.read_text(encoding="utf-8", errors="replace").splitlines()[-limit * 4 :]
    out: list[dict[str, object]] = []
    for line in lines:
        with contextlib.suppress(ValueError):
            if isinstance(row := json.loads(line), dict):
                out.append(row)
    return out


__all__ = ["LogTable", "LogToBus", "plugin_of", "read_file"]
