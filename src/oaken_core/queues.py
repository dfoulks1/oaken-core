# SPDX-License-Identifier: Apache-2.0
"""Named queues. One, ``default``, always exists and cannot be removed; more are made in code.

Jobs never pick a queue. A job goes where its subject's ``queue`` label says, or to ``default``
when there is no label. ``default`` is also where intake runs: only triagers should work there.
"""

import re
from dataclasses import dataclass

from oaken_core.errors import ConfigError, InvalidInput
from oaken_core.labels import LabelError

DEFAULT = "default"
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")


@dataclass(frozen=True)
class QueueSpec:
    name: str
    description: str = ""
    workers: int = 1  # concurrent workers the runner starts for this queue


class Queues:
    def __init__(self, default_workers: int = 2) -> None:
        self._queues: dict[str, QueueSpec] = {
            DEFAULT: QueueSpec(DEFAULT, "Where unlabeled work starts: intake and triage", default_workers)
        }

    def new_queue(self, name: str, description: str = "", workers: int = 1) -> QueueSpec:
        if not _NAME.match(name):
            raise ConfigError(f"queue name {name!r} must match {_NAME.pattern}")
        if name in self._queues:
            raise ConfigError(f"queue {name!r} already exists")
        if workers < 1:
            raise ConfigError("a queue needs at least one worker")
        spec = self._queues[name] = QueueSpec(name, description, workers)
        return spec

    def get(self, name: str) -> QueueSpec:
        try:
            return self._queues[name]
        except KeyError:
            raise InvalidInput(f"no queue named {name!r}") from None

    def all(self) -> list[QueueSpec]:
        return list(self._queues.values())

    def validate_label(self, value: str) -> None:
        """For ``LabelMaker.set_validator("queue", ...)``: the label must name a real queue."""
        if value not in self._queues:
            raise LabelError(f"no queue named {value!r}")
