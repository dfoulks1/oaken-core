# SPDX-License-Identifier: Apache-2.0
"""In-memory typed event bus.

Two kinds of listener, with different promises:

* **Handlers** (``subscribe``) are internal components. Every published event is delivered to
  every matching handler; none is ever dropped. A handler that raises is logged and skipped, and
  never affects the publisher or the other handlers. ``publish`` returns once all handlers have
  finished, so handlers should be quick (queue work as a job instead of doing it inline).
* **Streams** (``stream``) feed clients such as SSE. Each has a bounded queue. When a consumer is
  too slow and its queue is full, the **oldest** event is dropped to make room, and
  ``EventStream.dropped`` counts them. A slow stream never blocks ``publish``.
"""

import asyncio
import dataclasses
import json
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any, Self

from oaken_core.ids import JobId, uuid7
from oaken_core.logging import get_logger

log = get_logger("oaken_core.events")

DEFAULT_STREAM_QUEUE = 256


def _new_event_id() -> str:
    return str(uuid7())


@dataclass(frozen=True, kw_only=True)
class Event:
    ts: datetime
    event_id: str = field(default_factory=_new_event_id)


@dataclass(frozen=True, kw_only=True)
class TraceEvent(Event):
    """One line of the trace, which holds private text. It is published with ``publish_nowait``, so
    the persistent handlers (the event log, the SSE ring) never see it, and ``EventBus.stream``
    leaves it out unless a stream asks for ``TraceEvent`` by name. Only ``oaken trace -f`` (through
    the authenticated API) subscribes."""

    kind: str
    data: dict[str, Any]




@dataclass(frozen=True, kw_only=True)
class JobStateChanged(Event):
    job_id: JobId
    kind: str
    queue: str
    status: str
    subject: str | None = None


@dataclass(frozen=True, kw_only=True)
class QueueStateChanged(Event):
    queue: str
    paused: bool


@dataclass(frozen=True, kw_only=True)
class PluginHealthChanged(Event):
    plugin: str
    healthy: bool
    detail: str = ""


@dataclass(frozen=True, kw_only=True)
class PluginEvent(Event):
    """Base for events a plugin defines itself: subclass it, add plain-data fields, and publish
    with ``ctx.publish(...)``. ``plugin`` names the sender (the host fills it in)."""

    plugin: str = ""


def event_data(event: Event) -> dict[str, Any]:
    """The event as plain JSON data: its fields, timestamps in ISO 8601. Events hold ids and
    numbers, never private text; this is how both the SSE stream and the event log write one."""
    out: dict[str, Any] = {}
    for f in dataclasses.fields(event):
        value = getattr(event, f.name)
        out[f.name] = value.isoformat() if isinstance(value, datetime) else value
    data: dict[str, Any] = json.loads(json.dumps(out, default=str))
    return data


Handler = Callable[[Event], Awaitable[None]]
EventFilter = Callable[[Event], bool]


class EventStream:
    """An async iterator of events with a bounded, drop-oldest queue. Close it when done."""

    def __init__(self, bus: "EventBus", accept: EventFilter, maxsize: int) -> None:
        self._bus = bus
        self._accept = accept
        self._maxsize = maxsize
        self._items: deque[Event] = deque()
        self._ready = asyncio.Event()
        self._closed = False
        self.dropped = 0

    def _offer(self, event: Event) -> None:
        if self._closed or not self._accept(event):
            return
        if len(self._items) >= self._maxsize:
            self._items.popleft()
            self.dropped += 1
        self._items.append(event)
        self._ready.set()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Event:
        while True:
            if self._items:
                return self._items.popleft()
            if self._closed:
                raise StopAsyncIteration
            self._ready.clear()
            await self._ready.wait()

    async def aclose(self) -> None:
        self._closed = True
        self._bus._streams.discard(self)
        self._ready.set()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[tuple[type[Event], Handler]] = []
        self._streams: set[EventStream] = set()

    def subscribe(self, event_type: type[Event], handler: Handler) -> Callable[[], None]:
        """Call ``handler`` for every event that is an instance of ``event_type`` (``Event``
        matches all). Returns a function that removes the subscription."""
        entry = (event_type, handler)
        self._handlers.append(entry)

        def unsubscribe() -> None:
            if entry in self._handlers:
                self._handlers.remove(entry)

        return unsubscribe

    def stream(
        self,
        event_filter: EventFilter | type[Event] | tuple[type[Event], ...] | None = None,
        *,
        maxsize: int = DEFAULT_STREAM_QUEUE,
    ) -> EventStream:
        """Start receiving matching events now; iterate with ``async for``, then ``aclose()``."""
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1")
        accept: EventFilter
        if event_filter is None:

            def accept(e: Event) -> bool:
                return not isinstance(e, TraceEvent)  # private text: only by asking for it
        elif isinstance(event_filter, type | tuple):
            types = event_filter

            def accept(e: Event) -> bool:
                return isinstance(e, types)
        else:
            accept = event_filter
        stream = EventStream(self, accept, maxsize)
        self._streams.add(stream)
        return stream

    def publish_nowait(self, event: Event) -> None:
        """Offer ``event`` to the open streams only: no handler runs and nothing persists it. For
        events that hold private text (the trace). Never raises."""
        for stream in list(self._streams):
            try:
                stream._offer(event)
            except Exception:  # a broken filter must not reach the publisher
                log.exception("event stream filter failed", event_type=type(event).__name__)

    async def publish(self, event: Event) -> None:
        for stream in list(self._streams):
            try:
                stream._offer(event)
            except Exception:  # a broken filter must not reach the publisher
                log.exception("event stream filter failed", event_type=type(event).__name__)
        matching = [h for t, h in self._handlers if isinstance(event, t)]
        if matching:
            await asyncio.gather(*(self._call(h, event) for h in matching))

    @staticmethod
    async def _call(handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:
            log.exception("event handler failed", event_type=type(event).__name__)
