# SPDX-License-Identifier: Apache-2.0
"""Service units: a dependency-ordered start/stop for the composition root.

``Core.open()`` builds the object graph (construction order encodes dependencies). The *running*
of that graph — start in order, stop the started ones in reverse — is bookkeeping that used to be
split between a hand-appended ``_started`` list and a parallel name→handler dict in ``stop()``,
two things that had to agree. ``Lifecycle`` makes one ordered list the single source of truth for
both directions, so a started stage can never lack a stopper.

This module is **core-internal**: it is not exported from ``oaken_core.daemon`` and is not a public
seam. A second developer adds a subsystem by appending one ``ServiceUnit`` in ``open()``, not by
editing ``start()`` and ``stop()`` in two places.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from oaken_core.errors import OakenError
from oaken_core.logging import get_logger

log = get_logger("oaken_core.daemon")

# A unit's start/stop callable. A return value (e.g. a problems list from the plugin host) is
# accepted and discarded: the Lifecycle only cares that the coroutine completed.
AsyncFn = Callable[[], Awaitable[object]]


@runtime_checkable
class ServiceUnit(Protocol):
    """One startable/stoppable stage of core. ``name`` is the stage label used in logs and in the
    daemon's ``_started`` record (``host``, ``scheduler``, ``jobs``, ``mcp``, ``api``)."""

    name: str

    async def start(self) -> None:
        """Bring this stage up. Raising aborts bring-up; the Lifecycle stops the started ones."""
        ...

    async def stop(self) -> None:
        """Take this stage down. Must be safe to call on a stage that never started."""
        ...


@dataclass
class _Unit:
    """A ServiceUnit from two callables, so ``Core`` can build units inline without a class each."""

    name: str
    _start: AsyncFn
    _stop: AsyncFn

    async def start(self) -> None:
        await self._start()

    async def stop(self) -> None:
        await self._stop()


def unit(name: str, start: AsyncFn, stop: AsyncFn) -> ServiceUnit:
    """A ServiceUnit from a name and start/stop callables."""
    return _Unit(name, start, stop)


class Lifecycle:
    """Starts units in order and stops the started ones in reverse.

    ``started`` is the list of names brought up so far, in order — the daemon keeps a reference to
    it (its ``_started``) so a test and a half-built teardown can read exactly what is up. On a
    unit's ``start`` raising, bring-up aborts and the already-started units are stopped (reverse
    order); the exception propagates. ``stop`` is safe to call when nothing, or only some, started,
    and a failing ``stop`` is logged and does not prevent the others from stopping.
    """

    def __init__(self, units: list[ServiceUnit]) -> None:
        self._units = units
        self._by_name = {u.name: u for u in units}

    async def start(self, started: list[str]) -> None:
        """Start every unit in order, appending each name to ``started`` as it comes up. If one
        raises, stop what started (reverse) and re-raise the original error."""
        for u in self._units:
            try:
                await u.start()
            except BaseException:
                await self.stop(started)
                raise
            started.append(u.name)

    async def stop(self, started: list[str]) -> None:
        """Stop the started units in reverse order. Clears ``started`` as it goes, so the record
        reflects what is still up even if a stop raises. A failing stop is logged, not raised."""
        for name in reversed(started):
            unit_ = self._by_name.get(name)
            if unit_ is None:
                continue
            try:
                await unit_.stop()
            except (OakenError, OSError, RuntimeError):
                log.exception("stopping failed", step=name)
        started.clear()
