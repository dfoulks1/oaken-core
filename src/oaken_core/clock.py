# SPDX-License-Identifier: Apache-2.0
"""Injectable time, so tests never depend on the wall clock."""

from datetime import UTC, datetime, timedelta
from typing import Protocol

_TS = "%Y-%m-%dT%H:%M:%S.%fZ"


def fmt_ts(dt: datetime) -> str:
    """UTC, fixed width, so the text sorts the same as the time."""
    return dt.strftime(_TS)


def parse_ts(text: str) -> datetime:
    return datetime.strptime(text, _TS).replace(tzinfo=UTC)


class Clock(Protocol):
    def now(self) -> datetime:
        """The current time, timezone-aware, in UTC."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """A clock that only moves when told to."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        if self._now.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware start time")

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> datetime:
        """Move forward by a ``timedelta`` (``seconds=``, ``minutes=``, ...)."""
        self._now += timedelta(**kwargs)
        return self._now

    def set(self, when: datetime) -> None:
        if when.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware time")
        self._now = when
