# SPDX-License-Identifier: Apache-2.0
"""The seam between the CLI and core: a small, transport-neutral client.

The CLI never imports core's HTTP wiring or its in-process ``CoreClient`` directly. It programs
against :class:`ApiClient`, an abstract interface with exactly the operations a thin client needs:

* :meth:`ApiClient.call` — run one registry operation (``POST /v1/ops/{name}`` over HTTP);
* :meth:`ApiClient.list_ops` — the operation catalogue (``GET /v1/ops``), which drives both the
  generic ``oaken op NAME`` fallback and ``--help`` text;
* :meth:`ApiClient.stream_events` — the SSE event stream (``GET /v1/events``), for ``oaken watch``;
* :meth:`ApiClient.health` — a liveness probe (``GET /v1/health``) used by ``oaken status``.

Two implementations exist (or will):

* :class:`~oaken_core.cli.inprocess.InProcessApiClient` wraps the in-process ``CoreClient``. It needs no
  daemon and no socket, so the CLI's whole command surface is testable.
* ``HttpApiClient`` speaks to the running daemon; see :mod:`oaken_core.cli.http`.

The two results a mutation can have — it ran, or it needs confirming first — are modelled by
:class:`CallResult`, which mirrors core's own ``CallResult`` without importing it, so the HTTP
decoder and the in-process adapter produce the same shape.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class OpInfo:
    """One row of the operation catalogue, as a thin client needs it.

    This is the decoded form of a ``/v1/ops`` entry. It is deliberately a plain dataclass, not a
    pydantic model: the CLI must not import the registry to describe an operation, so that a
    registry change never forces a CLI release (the generic ``oaken op NAME`` fallback is built
    from exactly these fields).
    """

    name: str
    summary: str
    permission: str  # "read" | "mutate" | "send"
    group: str = ""
    plugin: str | None = None
    template: str | None = None  # the template this is an instance of; sets the 3-level CLI path
    agent_ok: bool = False
    streaming: bool = False
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)

    @property
    def cli_path(self) -> tuple[str, ...]:
        """Where the CLI puts it: ``(template, instance, verb)``, ``(plugin, verb)``,
        ``(group, verb)`` or ``(name,)``."""
        if self.plugin:
            verb = self.name.split(".", 1)[1]
            if self.template:
                return (self.template, self.plugin, verb)
            return (self.plugin, verb)
        if self.group:
            return (self.group, self.name[len(self.group) + 1 :])
        return (self.name,)


@dataclass(frozen=True)
class Proposal:
    """What a mutation *would* change, returned when it has not been confirmed.

    The CLI shows this to the person and, for an explicit ``--yes`` or an interactive confirm,
    calls again with ``confirmation_token``. Mirrors core's gate ``Proposal``; ``changes`` may be a
    prefix of the whole, and ``total_changes`` is always the real count.
    """

    operation: str
    summary: str
    changes: list[dict[str, Any]] = field(default_factory=list)
    total_changes: int = 0
    confirmation_token: str = ""
    expires_at: str = ""


@dataclass(frozen=True)
class CallResult:
    """The outcome of :meth:`ApiClient.call`: either ``output`` (it ran) or ``proposal`` (confirm
    first). ``request_id`` ties the call to an audit row and to the daemon's logs."""

    request_id: str
    output: Any | None = None
    proposal: Proposal | None = None

    @property
    def executed(self) -> bool:
        return self.proposal is None


@dataclass(frozen=True)
class Event:
    """One line of the SSE stream: a typed event name, its JSON data, and the stream id used to
    resume with ``Last-Event-ID`` after a reconnect."""

    id: str
    event: str
    data: Mapping[str, Any]


class ApiClient(ABC):
    """The thin-client contract. An implementation decides *how* it reaches core (HTTP to the
    daemon, or the in-process registry); the CLI only ever uses these methods.

    Lifetime: an ``ApiClient`` is an async context manager so an HTTP implementation can hold a
    connection pool and close it cleanly. The in-process implementation needs no teardown but
    honours the same protocol.
    """

    async def __aenter__(self) -> ApiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:  # noqa: B027 — an optional concrete hook, not abstract
        """Release any resources (HTTP pool, etc.). The default does nothing."""

    @abstractmethod
    async def health(self) -> Mapping[str, Any]:
        """Liveness only. Never requires a working operation; used by ``oaken status`` to tell a
        running-but-degraded core from one that is down. Raises :class:`CoreUnavailable` when core
        cannot be reached at all."""

    @abstractmethod
    async def list_ops(self) -> list[OpInfo]:
        """The operations this caller may invoke, each with its schemas. Drives the generic
        ``oaken op NAME`` fallback and the auto-derived ``--help``."""

    @abstractmethod
    async def call(
        self,
        name: str,
        payload: Mapping[str, Any] | None = None,
        *,
        confirm: bool = False,
        confirmation_token: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> CallResult:
        """Run one operation. ``timeout`` (seconds) replaces the transport's default wait for the
        answer, for the few operations that are slow by nature, such as ``ask`` on a local model.

        ``confirm=True`` is the CLI saying *the person at the terminal asked for this exact
        action*; it is how a human-class caller pre-confirms a mutation. A mutation that is neither
        confirmed nor accompanied by a ``confirmation_token`` comes back as a :class:`Proposal` and
        changes nothing.
        """

    @abstractmethod
    def stream_trace(self, filters: dict[str, Any]) -> AsyncIterator[Event]:
        """Yield trace events as they are emitted, filtered by ``plugin``, ``job_id``,
        ``subject`` and ``kinds`` (the ``/v1/trace/stream`` route). Each event's ``data`` is
        ``{"ts", "kind", "data"}``. Authenticated: the trace holds private text."""

    @abstractmethod
    def stream_events(self, last_event_id: str | None = None) -> AsyncIterator[Event]:
        """Yield events as they arrive, resuming after ``last_event_id`` when given. Returns an
        async iterator (not a coroutine) so ``oaken watch`` can ``async for`` over it."""
