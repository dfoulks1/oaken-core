# SPDX-License-Identifier: Apache-2.0
"""Named queues. One, ``default``, always exists and cannot be removed; more are made in code or in
``config.toml`` (``[queues.<name>]``). A made queue serves one analyzer kind: it is a pipeline stage.

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
    kind: str = ""  # the one analyzer plugin kind this queue is the stage of ("" for ``default``)
    label: str = ""  # its title in the console

    @property
    def title(self) -> str:
        return self.label or self.name.replace("-", " ").replace("_", " ").capitalize()


class Queues:
    def __init__(self, default_workers: int = 2) -> None:
        self._queues: dict[str, QueueSpec] = {
            DEFAULT: QueueSpec(DEFAULT, "Where unlabeled work starts: intake and triage", default_workers)
        }

    def new_queue(self, name: str, description: str = "", workers: int = 1, *, kind: str = "", label: str = "") -> QueueSpec:
        if not _NAME.match(name):
            raise ConfigError(f"queue name {name!r} must match {_NAME.pattern}")
        if name in self._queues:
            raise ConfigError(f"queue {name!r} already exists")
        if workers < 1:
            raise ConfigError("a queue needs at least one worker")
        if kind and (other := next((q.name for q in self._queues.values() if q.kind == kind), None)):
            raise ConfigError(f"queue {other!r} already serves {kind!r}: a plugin kind belongs to one queue, so each plugin has one stage")
        spec = self._queues[name] = QueueSpec(name, description, workers, kind, label)
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
