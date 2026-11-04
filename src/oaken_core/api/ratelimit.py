# SPDX-License-Identifier: Apache-2.0
"""A token bucket per client, for mutating requests. In memory, bounded, and driven by a monotonic
clock that a test can replace."""

import time
from collections.abc import Callable

MAX_CLIENTS = 1024  # buckets kept; the least recently used go first


class RateLimiter:
    def __init__(
        self,
        per_minute: int,
        *,
        now: Callable[[], float] = time.monotonic,
        burst: int | None = None,
    ) -> None:
        self.rate = per_minute / 60.0
        self.burst = float(burst if burst is not None else max(10, per_minute // 10))
        self._now = now
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last seen)

    def take(self, key: str) -> float:
        """Spend one token. Returns 0 if allowed, else the seconds until one is free."""
        now = self._now()
        tokens, last = self._buckets.pop(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        wait = 0.0
        if tokens >= 1.0:
            tokens -= 1.0
        else:
            wait = (1.0 - tokens) / self.rate
        self._buckets[key] = (tokens, now)  # re-inserted last: dicts keep insertion order
        while len(self._buckets) > MAX_CLIENTS:
            del self._buckets[next(iter(self._buckets))]
        return wait
