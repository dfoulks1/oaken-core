# SPDX-License-Identifier: Apache-2.0
"""An :class:`~oaken_core.cli.client.ApiClient` that calls core in process.

This is how the CLI runs in tests, and how anyone can drive the whole command surface without a
daemon or a socket. It wraps a ``CoreClient`` — the same object the HTTP server will
wrap later — so the two transports share one definition of what an operation does and returns.

The CLI stays ignorant of core's internals: it hands this adapter a built ``CoreClient`` (the test
rig builds one over the fakes) and then speaks only the :class:`ApiClient` protocol. The adapter's
only job is to translate between core's in-process types (``registry`` operations, the gate
``Proposal``, event dataclasses) and the transport-neutral shapes in :mod:`oaken_core.cli.client`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any

from oaken_core.cli.client import ApiClient, CallResult, Event, OpInfo, Proposal
from oaken_core.operations.caller import CallerContext

if TYPE_CHECKING:
    from oaken_core.events import Event as CoreEvent
    from oaken_core.operations.client import CoreClient


class InProcessApiClient(ApiClient):
    """Drive ``CoreClient`` directly, as the CLI caller class.

    ``caller`` is fixed to the CLI class (a human acting for themselves, who may pre-confirm their
    own mutations) and is never taken from anything a command passes in — the transport decides the
    class, not the caller (see ``oaken_core.operations.caller``).
    """

    def __init__(self, core: CoreClient) -> None:
        self._core = core
        # A fresh request id per call; the CLI is always a human-class caller.
        self._caller_factory = CallerContext.cli

    async def health(self) -> Mapping[str, Any]:
        # In process there is no socket to fail: if we hold a CoreClient, core is "up". Report the
        # version the services know, matching the shape the HTTP /v1/health will return.
        return {"status": "ok", "version": self._core.svc.version}

    async def list_ops(self) -> list[OpInfo]:
        caller = self._caller_factory()
        ops = await self._core.visible(caller)
        return [
            OpInfo(
                name=op.name,
                summary=op.summary,
                permission=op.permission,
                group=op.group,
                plugin=op.plugin,
                template=op.template,
                agent_ok=op.agent_ok,
                streaming=op.streaming,
                input_schema=op.input_schema(),
                output_schema=op.output_schema(),
            )
            for op in ops
        ]

    async def call(
        self,
        name: str,
        payload: Mapping[str, Any] | None = None,
        *,
        confirm: bool = False,
        confirmation_token: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 (in process there is no transport to time)
    ) -> CallResult:
        result = await self._core.call(
            name,
            dict(payload or {}),
            self._caller_factory(),
            confirm=confirm,
            confirmation_token=confirmation_token,
        )
        proposal = None
        if result.proposal is not None:
            p = result.proposal
            proposal = Proposal(
                operation=p.operation,
                summary=p.summary,
                # The gate's ``changes`` are pydantic ``Change`` models; JSON-dump each.
                changes=[c.model_dump(mode="json") for c in p.changes],
                total_changes=p.total_changes,
                confirmation_token=p.confirmation_token,
                expires_at=str(p.expires_at),
            )
        output = None
        if result.output is not None:
            output = result.output.model_dump(mode="json")
        return CallResult(request_id=result.request_id, output=output, proposal=proposal)

    async def stream_trace(self, filters: dict[str, Any]) -> AsyncIterator[Event]:
        from oaken_core.api.app import matches, want_from
        from oaken_core.events import TraceEvent

        bus = self._core.svc.bus
        if bus is None:
            return
        want = want_from(
            plugin=filters.get("plugin"), job_id=filters.get("job_id"), subject=filters.get("subject")
        )
        kinds = set(filters.get("kinds") or [])
        stream = bus.stream(
            lambda e: (
                isinstance(e, TraceEvent)
                and matches(e.data, want)
                and (not kinds or e.kind in kinds)
            )
        )
        try:
            async for ev in stream:
                if isinstance(ev, TraceEvent):
                    row = {"ts": ev.ts.isoformat(), "kind": ev.kind, "data": ev.data}
                    yield Event(id=ev.event_id, event="trace", data=row)
        finally:
            await stream.aclose()

    async def stream_events(self, last_event_id: str | None = None) -> AsyncIterator[Event]:
        bus = self._core.svc.bus
        if bus is None:
            return
        stream = bus.stream()
        try:
            async for core_event in stream:
                yield _to_event(core_event)
        finally:
            await stream.aclose()


def _to_event(core_event: CoreEvent) -> Event:
    """Turn a core event dataclass into the transport-neutral :class:`Event`.

    The event *name* is the dataclass name (``SyncStarted`` → ``sync_started`` style is left to the
    HTTP layer; here we keep the class name so in-process consumers see exactly the type). Data is
    the dataclass fields, JSON-ready; ``event_id`` becomes the stream id.
    """
    data = dataclasses.asdict(core_event) if dataclasses.is_dataclass(core_event) else {}
    # Make the timestamp JSON-friendly and keep the rest as-is.
    if "ts" in data and hasattr(data["ts"], "isoformat"):
        data["ts"] = data["ts"].isoformat()
    event_id = str(data.get("event_id", ""))
    return Event(id=event_id, event=type(core_event).__name__, data=data)
