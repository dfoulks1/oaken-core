# SPDX-License-Identifier: Apache-2.0
"""Labels: key-value marks on a subject, in two layers.

A subject is anything with a ``SubjectId`` (core never learns what). Each key has a *human* value
and a *machine* value. The effective value is the human one if set, else the machine one. A person
writes the human layer; an agent or core writes the machine layer. A machine re-run therefore
cannot overwrite a person's decision, and clearing the human layer is the unlock. Both layers and
the actor are logged. A write that changes nothing writes nothing and logs nothing.

Keys must be registered first. Core reserves ``queue``, ``priority`` and ``confidential``; a plugin
may write them but not define them. A bad write raises ``LabelError`` (the host disables a plugin
that causes one).
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Self

from oaken_core.actors import Actor
from oaken_core.changelog import ChangeLog
from oaken_core.clock import Clock, fmt_ts
from oaken_core.db import Database
from oaken_core.errors import OakenError

KEY = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")


class LabelError(OakenError):
    code = "label_error"


@dataclass(frozen=True)
class BaseLabel:
    """A label key a plugin defines. ``values`` empty means any string."""

    key: str
    description: str = ""
    values: tuple[str, ...] = ()
    reserved: ClassVar[bool] = False

    @classmethod
    def new(cls, key: str, description: str = "", values: tuple[str, ...] = ()) -> Self:
        if not KEY.match(key):
            raise LabelError(f"label key {key!r} must match {KEY.pattern}")
        return cls(key, description, tuple(values))


@dataclass(frozen=True)
class ReservedLabel(BaseLabel):
    """A key core owns. Plugins can write it and cannot define it."""

    reserved: ClassVar[bool] = True


Validator = Callable[[str], None]  # raises LabelError


class LabelMaker:
    """The registry of label keys. A core service."""

    def __init__(self) -> None:
        self._labels: dict[str, BaseLabel] = {}
        self._validators: dict[str, Validator] = {}

    @classmethod
    def core(cls) -> "LabelMaker":
        """A maker holding core's reserved keys."""
        maker = cls()
        for label in (
            ReservedLabel.new("queue", "The queue a job is routed to"),
            ReservedLabel.new("priority", "Urgency", ("urgent", "high", "normal", "low")),
            ReservedLabel.new("confidential", "Keep off remote models", ("true", "false")),
        ):
            maker._labels[label.key] = label
        return maker

    def register(self, label: BaseLabel) -> None:
        """Define a key. Re-registering the identical definition is allowed (a plugin reload)."""
        have = self._labels.get(label.key)
        if have is not None:
            if have.reserved:
                raise LabelError(f"{label.key!r} is reserved by core")
            if have != label:
                raise LabelError(f"{label.key!r} is already defined differently")
            return
        if label.reserved:
            raise LabelError(f"only core reserves keys; {label.key!r} is not one of core's")
        self._labels[label.key] = label

    def set_validator(self, key: str, check: Validator) -> None:
        """Add a check to a registered key, run on every write (``queue`` must name a real queue)."""
        self.get(key)
        self._validators[key] = check

    def get(self, key: str) -> BaseLabel:
        try:
            return self._labels[key]
        except KeyError:
            raise LabelError(f"unknown label key {key!r}") from None

    def check(self, key: str, value: str) -> None:
        label = self.get(key)
        if label.values and value not in label.values:
            raise LabelError(f"{key}: {value!r} is not one of {', '.join(label.values)}")
        if key in self._validators:
            self._validators[key](value)


HUMAN, MACHINE = "human", "machine"


def layer_of(actor: Actor) -> str:
    return HUMAN if actor.kind == "user" else MACHINE


class LabelStore:
    def __init__(self, db: Database, maker: LabelMaker, log: ChangeLog, clock: Clock) -> None:
        self.db, self.maker, self.log, self.clock = db, maker, log, clock

    async def set(self, subject_id: str, key: str, value: str, actor: Actor) -> bool:
        """Write ``value`` to the actor's layer. False (and no log row) if nothing changed."""
        self.maker.check(key, value)
        layer = layer_of(actor)
        async with self.db.transaction() as tx:
            row = await tx.fetchone(
                "SELECT value FROM labels WHERE subject_id = ? AND key = ? AND layer = ?",
                (subject_id, key, layer),
            )
            old = row["value"] if row else None
            if old == value:
                return False
            await tx.execute(
                "INSERT INTO labels (subject_id, key, layer, value, actor_kind, actor, at)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT (subject_id, key, layer) DO UPDATE SET"
                " value = excluded.value, actor_kind = excluded.actor_kind,"
                " actor = excluded.actor, at = excluded.at",
                (subject_id, key, layer, value, actor.kind, actor.label, fmt_ts(self.clock.now())),
            )
            await self.log.append(
                tx, actor=actor, subject_id=subject_id, key=key, op="set", layer=layer,
                old=old, new=value,
            )  # fmt: skip
        return True

    async def get(self, subject_id: str, key: str) -> str | None:
        """The effective value: human if set, else machine."""
        row = await self.db.fetchone(
            "SELECT value FROM labels WHERE subject_id = ? AND key = ?"
            " ORDER BY layer = 'human' DESC LIMIT 1",
            (subject_id, key),
        )
        return row["value"] if row else None

    async def layers(self, subject_id: str, key: str) -> dict[str, str]:
        """Both values. When they differ, the machine disagrees with the person."""
        rows = await self.db.fetchall(
            "SELECT layer, value FROM labels WHERE subject_id = ? AND key = ?", (subject_id, key)
        )
        return {r["layer"]: r["value"] for r in rows}

    async def all(self, subject_id: str) -> dict[str, str]:
        rows = await self.db.fetchall(
            "SELECT key, layer, value FROM labels WHERE subject_id = ?", (subject_id,)
        )
        out: dict[str, str] = {}
        for r in sorted(rows, key=lambda r: r["layer"] == HUMAN):  # human rows overwrite last
            out[r["key"]] = r["value"]
        return out

    async def clear_human(self, subject_id: str, key: str, actor: Actor) -> bool:
        """Remove the person's value so the machine's shows again. This is the unlock."""
        return bool(await self._clear(subject_id, HUMAN, actor, key))

    async def clear_machine(self, subject_id: str, actor: Actor, key: str | None = None) -> int:
        """Remove machine values (one key, or all of the subject's), keeping the human layer.
        Used by a retriage. Returns how many were removed."""
        return await self._clear(subject_id, MACHINE, actor, key)

    async def _clear(self, subject_id: str, layer: str, actor: Actor, key: str | None) -> int:
        async with self.db.transaction() as tx:
            sql = "SELECT key, value FROM labels WHERE subject_id = ? AND layer = ?"
            args: list[str] = [subject_id, layer]
            if key is not None:
                sql += " AND key = ?"
                args.append(key)
            rows = await tx.fetchall(sql, args)
            for r in rows:
                await tx.execute(
                    "DELETE FROM labels WHERE subject_id = ? AND key = ? AND layer = ?",
                    (subject_id, r["key"], layer),
                )
                await self.log.append(
                    tx, actor=actor, subject_id=subject_id, key=r["key"], op="clear", layer=layer,
                    old=r["value"],
                )  # fmt: skip
        return len(rows)
