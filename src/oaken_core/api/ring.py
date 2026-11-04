# SPDX-License-Identifier: Apache-2.0
"""The event stream as clients see it: Server-Sent Events with ids and a small replay buffer.

Core's ``EventBus`` has no ids and drops nothing for handlers, but a client that reconnects needs to
ask "what did I miss". ``EventRing`` is one handler on the bus that numbers every event and
last N. A client that sends ``Last-Event-ID`` gets what is still in the ring after that id; if the
id is older than the ring (or from a previous run of core) it is told so with a ``gap`` event and
should refresh what it shows. Events hold ids and numbers, never private text.
"""

import asyncio
import dataclasses
import json
from collections import deque
from collections.abc import AsyncIterator, Callable

from oaken_core.events import Event, EventBus, event_data


@dataclasses.dataclass(frozen=True)
class Frame:
    id: int
    name: str
    data: str  # one line of JSON

    def encode(self) -> str:
        return f"id: {self.id}\nevent: {self.name}\ndata: {self.data}\n\n"


class EventRing:
    def __init__(self, size: int = 1000) -> None:
        self._frames: deque[Frame] = deque(maxlen=size)
        self._seq = 0
        self._changed = asyncio.Event()
        self._closed = False

    @property
    def last_id(self) -> int:
        return self._seq

    def attach(self, bus: EventBus) -> Callable[[], None]:
        async def on_event(event: Event) -> None:
            self.append(event)

        return bus.subscribe(Event, on_event)

    def append(self, event: Event) -> Frame:
        self._seq += 1
        frame = Frame(self._seq, type(event).__name__, json.dumps(event_data(event)))
        self._frames.append(frame)
        woken, self._changed = self._changed, asyncio.Event()
        woken.set()
        return frame

    def after(self, last_id: int) -> tuple[list[Frame], bool]:
        """Frames newer than ``last_id``, and whether some were lost before them."""
        if last_id > self._seq:
            return [], True  # an id from before this run: nothing here can follow it
        first = self._frames[0].id if self._frames else self._seq + 1
        return [f for f in self._frames if f.id > last_id], last_id + 1 < first

    def close(self) -> None:
        """End every open stream (so a shutdown does not wait on them)."""
        self._closed = True
        self._changed.set()

    async def stream(self, last_id: int | None, heartbeat: float) -> AsyncIterator[str]:
        """The wire text of the stream, forever (until ``close``). A fresh connection sees only
        what happens after it; a reconnect sees what it missed first."""
        cursor = self._seq if last_id is None else last_id
        yield ": connected\n\n"
        while not self._closed:
            changed = self._changed  # taken before looking, so nothing can slip between
            frames, gap = self.after(cursor)
            if gap:
                first = self._frames[0].id if self._frames else self._seq + 1
                yield f"event: gap\ndata: {json.dumps({'first_available': first})}\n\n"
                cursor = max(0, first - 1) if cursor <= self._seq else self._seq
                frames, _ = self.after(cursor)
            for frame in frames:
                cursor = frame.id
                yield frame.encode()
            if frames or gap:
                continue
            try:
                await asyncio.wait_for(changed.wait(), heartbeat)
            except TimeoutError:
                yield ": heartbeat\n\n"
