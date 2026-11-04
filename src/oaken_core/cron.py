# SPDX-License-Identifier: Apache-2.0
"""A small five-field cron parser (minute hour day-of-month month day-of-week).

Supported per field: ``*``, numbers, ``a-b`` ranges, ``a,b`` lists and ``/n`` steps (``*/5``,
``10-30/5``). Months and weekdays also take three-letter names (``jan``, ``mon``). Weekday 0 and
7 are both Sunday. As in classic cron, when both day-of-month and day-of-week are restricted a
day matches if *either* matches. There is no ``@daily`` shorthand and no seconds. A schedule is
read in UTC unless ``next_after`` is given a time zone (from the system's tz database).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

from oaken_core.errors import ConfigError

_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_DAYS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}
_MAX_YEARS = 5


@dataclass(frozen=True)
class CronSpec:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    days_restricted: bool
    weekdays_restricted: bool

    def _day_ok(self, dt: datetime) -> bool:
        dom = dt.day in self.days
        dow = (dt.weekday() + 1) % 7 in self.weekdays
        if self.days_restricted and self.weekdays_restricted:
            return dom or dow
        return dom and dow

    def next_after(self, after: datetime, tz: tzinfo = UTC) -> datetime:
        """The first matching minute strictly after ``after`` (timezone-aware), with the fields
        read as wall-clock time in ``tz``. The result is in UTC.

        Around a daylight-saving change: a wall-clock time that does not exist (the hour skipped
        in spring) never matches, so ``30 2 * * *`` does not run that day; a time that happens
        twice (the hour repeated in autumn) matches its first occurrence only."""
        if after.tzinfo is None:
            raise ValueError("next_after needs a timezone-aware datetime")
        local = after.astimezone(tz).replace(tzinfo=None, second=0, microsecond=0)
        dt = local + timedelta(minutes=1)
        while True:
            found = self._next_wall(dt)
            when = _resolve(found, tz)
            if when is not None and when > after:
                return when
            dt = found + timedelta(minutes=1)

    def _next_wall(self, dt: datetime) -> datetime:
        """The first matching wall-clock minute at or after ``dt`` (naive)."""
        limit = dt + timedelta(days=366 * _MAX_YEARS)
        while dt < limit:
            if dt.month not in self.months:
                dt = _first_of_next_month(dt)
            elif not self._day_ok(dt):
                dt = (dt + timedelta(days=1)).replace(hour=0, minute=0)
            elif dt.hour not in self.hours:
                dt = (dt + timedelta(hours=1)).replace(minute=0)
            elif dt.minute not in self.minutes:
                dt += timedelta(minutes=1)
            else:
                return dt
        raise ConfigError("cron expression never matches (e.g. 30 February)")


def _resolve(wall: datetime, tz: tzinfo) -> datetime | None:
    """A naive wall-clock time as UTC: its first occurrence, or None if the clocks skipped it."""
    when = wall.replace(tzinfo=tz, fold=0).astimezone(UTC)
    return when if when.astimezone(tz).replace(tzinfo=None) == wall else None


def _first_of_next_month(dt: datetime) -> datetime:
    year, month = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return dt.replace(year=year, month=month, day=1, hour=0, minute=0)


def _number(text: str, names: dict[str, int], expr: str) -> int:
    if text.lower() in names:
        return names[text.lower()]
    if text.isdigit():
        return int(text)
    raise ConfigError(f"invalid cron value {text!r} in {expr!r}")


def _field(text: str, lo: int, hi: int, names: dict[str, int], expr: str) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        base, _, step_text = part.partition("/")
        if step_text and not step_text.isdigit():
            raise ConfigError(f"invalid cron step {step_text!r} in {expr!r}")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise ConfigError(f"cron step must be at least 1 in {expr!r}")
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, _, b = base.partition("-")
            start, end = _number(a, names, expr), _number(b, names, expr)
        else:
            start = _number(base, names, expr)
            end = hi if step_text else start
        if not (lo <= start <= end <= hi):
            raise ConfigError(f"cron value out of range {lo}-{hi} in {expr!r}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def parse_cron(expr: str) -> CronSpec:
    fields = expr.split()
    if len(fields) != 5:
        raise ConfigError(f"cron expression needs 5 fields, got {len(fields)}: {expr!r}")
    minute, hour, dom, month, dow = fields
    weekdays = _field(dow, 0, 7, _DAYS, expr)
    return CronSpec(
        minutes=_field(minute, 0, 59, {}, expr),
        hours=_field(hour, 0, 23, {}, expr),
        days=_field(dom, 1, 31, {}, expr),
        months=_field(month, 1, 12, _MONTHS, expr),
        weekdays=frozenset(0 if d == 7 else d for d in weekdays),
        days_restricted=dom != "*",
        weekdays_restricted=dow != "*",
    )
