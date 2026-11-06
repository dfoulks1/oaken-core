# SPDX-License-Identifier: Apache-2.0
"""Named queues. One, ``default``, always exists and cannot be removed; more are made in code or in
``config.toml`` (``[queues.<name>]``). A made queue serves one analyzer kind: it is a pipeline stage.

Jobs never pick a queue. A job goes where its subject's ``queue`` label says, or to ``default``
when there is no label. ``default`` is also where intake runs: only triagers should work there.
"""

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from oaken_core.actors import SYSTEM, Actor
from oaken_core.errors import ConfigError, InvalidInput
from oaken_core.labels import LabelError

if TYPE_CHECKING:
    from oaken_core.jobs import Enqueued, JobSpec, Jobs, Metrics, Stats

DEFAULT = "default"
STORE = "store"  # hidden, just after default: items labelled ``store`` wait here to be stored
EMBED = "embed"  # hidden, after store: items labelled ``embed`` wait here for their vector
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")


@dataclass(frozen=True)
class Queue:
    """One queue: its settings, and the jobs in it. It takes a job (``submit``); ``Jobs`` is the store behind it."""

    name: str
    description: str = ""
    workers: int = 1  # concurrent workers the runner starts for this queue
    kind: str = ""  # the one analyzer plugin kind this queue is the stage of ("" for ``default``)
    label: str = ""  # its title in the console
    hidden: bool = False  # a stage core runs for you: not listed, not edited
    disabled: tuple[str, ...] = ()  # plugins switched off in this stage only (still on in another)

    _jobs: "Jobs | None" = field(default=None, compare=False, repr=False)  # set by ``Queues.bind``

    @property
    def title(self) -> str:
        return self.label or self.name.replace("-", " ").replace("_", " ").capitalize()

    @property
    def jobs(self) -> "Jobs":
        if self._jobs is None:
            raise InvalidInput(f"queue {self.name!r} has no job store yet")
        return self._jobs

    def check(self, job: "JobSpec") -> None:
        """``InvalidInput`` for a payload that is not a JSON object."""
        try:
            ok = isinstance(json.loads(job.payload), dict)
        except ValueError:
            ok = False
        if not ok:
            raise InvalidInput("payload must be a JSON object")

    async def submit(self, job: "JobSpec", *, actor: Actor = SYSTEM) -> "Enqueued":
        """Put a job in this queue. The queue takes the job, not the other way round."""
        self.check(job)
        return await self.jobs.enqueue(job.kind, job.payload, queue=self.name, subject=job.subject, priority=job.priority, actor=actor)

    async def pause(self) -> None:
        await self.jobs.pause(self.name)

    async def resume(self) -> None:
        await self.jobs.resume(self.name)

    async def stats(self) -> "Stats":
        return next(r for r in await self.jobs.stats() if r.queue == self.name)

    async def metrics(self, window_seconds: int = 3600) -> "Metrics":
        return await self.jobs.metrics(self.name, window_seconds)


class Queues:
    def __init__(self, default_workers: int = 2) -> None:
        self._jobs: "Jobs | None" = None
        self._queues: dict[str, Queue] = {
            DEFAULT: Queue(DEFAULT, "Where an item enters: formatters split it into parts, then it moves on to triage", default_workers, kind="formatter", label="Intake"),
            STORE: Queue(STORE, "Items labelled store wait here to be stored", 1, hidden=True),
            EMBED: Queue(EMBED, "Items labelled embed wait here for their vector", 1, hidden=True),
        }

    def new_queue(self, name: str, description: str = "", workers: int = 1, *, kind: str = "", label: str = "", disabled: tuple[str, ...] = ()) -> Queue:
        if not _NAME.match(name):
            raise ConfigError(f"queue name {name!r} must match {_NAME.pattern}")
        if name in self._queues:
            raise ConfigError(f"queue {name!r} already exists")
        if workers < 1:
            raise ConfigError("a queue needs at least one worker")
        spec = self._queues[name] = Queue(name, description, workers, kind, label, disabled=tuple(disabled), _jobs=self._jobs)
        return spec

    def bind(self, jobs: "Jobs") -> None:
        """Give every queue (and those made later) the store its jobs live in."""
        self._jobs = jobs
        for name, q in self._queues.items():
            object.__setattr__(q, "_jobs", jobs)

    def get(self, name: str) -> Queue:
        try:
            return self._queues[name]
        except KeyError:
            raise InvalidInput(f"no queue named {name!r}") from None

    def all(self) -> list[Queue]:
        return list(self._queues.values())

    def set_disabled(self, name: str, disabled: tuple[str, ...]) -> None:
        """Change which plugins are off in a stage, now (a ``Queue`` is frozen, so this sets the field the way ``bind`` does)."""
        object.__setattr__(self.get(name), "disabled", tuple(disabled))

    def validate_label(self, value: str) -> None:
        """For ``LabelMaker.set_validator("queue", ...)``: the label must name a real queue."""
        if value not in self._queues:
            raise LabelError(f"no queue named {value!r}")
