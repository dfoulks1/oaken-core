# SPDX-License-Identifier: Apache-2.0
"""The trace: what a model or an agent is told, thinks and returns, with the full text.

It is for a person tuning triage and enrichment, who needs to see the prompt a model was given,
whether it reasoned, what it answered, how long it took and what was committed back. Because it
holds private text it is **off by default** (``[log] trace``, or ``oaken trace on``), it goes to a file
of its own (owner-only, rotated like the event log, never the audit log), and it is read only
through the authenticated API. No console page shows it (ADR 0013: counts and health only), and it
is never offered over MCP.

``Tracer`` is a ``EventLog`` with a switch: the same line format (``{"ts", "kind", "data"}``), the
same rotation and the same ``tail``. ``emit`` costs nothing while it is off.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from oaken_core.clock import Clock
from oaken_core.eventlog import EventLog
from oaken_core.events import EventBus, TraceEvent

# Who and which job an event happened under. Every event emitted while a context is set carries it,
# so a trace can be followed by plugin (``plugin_kind`` + ``plugin``) or by one job (``job_id``)
# without each call site knowing either.
_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("trace_context", default=None)


@contextmanager
def trace_context(**fields: Any) -> Iterator[None]:
    token = _CONTEXT.set(
        {**(_CONTEXT.get() or {}), **{k: v for k, v in fields.items() if v is not None}}
    )
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def brief(value: Any, depth: int = 0) -> Any:
    """A JSON-safe picture of a call's argument or result, with the text in it: private text (an
    ``UntrustedText``) is unwrapped because seeing it is the point of a trace; objects are shown by
    their fields; long lists are cut."""
    if depth > 5:
        return f"<{type(value).__name__}>"
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    unwrap = getattr(value, "unwrap", None)
    if callable(unwrap):
        return unwrap()
    if isinstance(value, dict):
        return {str(k): brief(v, depth + 1) for k, v in list(value.items())[:50]}
    if isinstance(value, list | tuple | set | frozenset):
        items = [brief(v, depth + 1) for v in list(value)[:50]]
        return items if len(value) <= 50 else [*items, f"... {len(value) - 50} more"]
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: brief(getattr(value, f.name), depth + 1) for f in fields(value)}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return brief(dump(), depth + 1)
    if isinstance(value, datetime):
        return value.isoformat()
    return repr(value)[:500]


class Tracer(EventLog):
    def __init__(
        self,
        path: Path,
        clock: Clock,
        *,
        max_bytes: int,
        backups: int,
        enabled: bool = False,
        cap: int = 200_000,
    ) -> None:
        super().__init__(path, max_bytes=max_bytes, backups=backups)
        self.clock = clock
        self.enabled = enabled
        self.cap = cap
        self.bus: EventBus | None = None  # set by core: live subscribers (`oaken trace -f`)

    def emit(self, kind: str, /, **data: Any) -> None:
        """Record one event, if tracing is on. Text longer than the cap is cut and says so."""
        if not self.enabled:
            return
        row = self._cut({**(_CONTEXT.get() or {}), **data})
        now = self.clock.now()
        self.append_line(now.isoformat(timespec="milliseconds"), kind, self._lean(kind, row))
        if self.bus is not None:  # to the open trace streams only; never to a persisting handler
            self.bus.publish_nowait(TraceEvent(ts=now, kind=kind, data=row))

    @staticmethod
    def _lean(kind: str, row: dict[str, Any]) -> dict[str, Any]:
        """The row as written to the file. A model call drops its empty fields; a None elsewhere
        means something (a plugin that returned nothing), so other events keep theirs."""
        return {k: v for k, v in row.items() if v is not None} if kind == "llm.call" else row

    def _cut(self, value: Any) -> Any:
        if isinstance(value, str):
            if len(value) <= self.cap:
                return value
            return f"{value[: self.cap]}...[cut {len(value) - self.cap} characters]"
        if isinstance(value, dict):
            return {str(k): self._cut(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self._cut(v) for v in value]
        if isinstance(value, datetime):
            return value.isoformat()
        return value
