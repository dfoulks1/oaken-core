# SPDX-License-Identifier: Apache-2.0
"""What each plugin has been asked, since this process started: calls, errors and how long they took.

Counted by ``BasePlugin`` around every call core makes (``TRACED``), so a plugin author writes nothing. Numbers only, never
text, so the console may show them (the trace, which has the text, is off by default). They live in memory and start at
zero with the process; the durable figures (a triager's verdicts, a model's tokens) come from core's tables.
"""

import time
from dataclasses import dataclass


@dataclass
class PluginStats:
    calls: int = 0
    errors: int = 0
    ms: float = 0.0  # total time spent in calls
    last_at: float | None = None  # wall-clock seconds of the last call

    @property
    def avg_ms(self) -> float:
        return self.ms / self.calls if self.calls else 0.0


_STATS: dict[tuple[str, str], PluginStats] = {}


def record(kind: str, name: str, ms: float, error: bool) -> None:
    s = _STATS.setdefault((kind, name), PluginStats())
    s.calls += 1
    s.errors += int(error)
    s.ms += ms
    s.last_at = time.time()


def get(kind: str, name: str) -> PluginStats:
    return _STATS.get((kind, name), PluginStats())
