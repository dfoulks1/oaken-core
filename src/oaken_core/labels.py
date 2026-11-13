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
    # Who may set or clear it, as an actor kind (``user``, ``agent``, ``system``) or one actor
    # (``system:mailrules``). Empty: anyone. A guard against a stage's label being applied by accident.
    writers: tuple[str, ...] = ()
    reserved: ClassVar[bool] = False

    @classmethod
    def new(cls, key: str, description: str = "", values: tuple[str, ...] = (), writers: tuple[str, ...] = ()) -> Self:
        if not KEY.match(key):
            raise LabelError(f"label key {key!r} must match {KEY.pattern}")
        return cls(key, description, tuple(values), tuple(writers))

    def may_write(self, actor: Actor) -> bool:
        return not self.writers or actor.kind in self.writers or f"{actor.kind}:{actor.label}" in self.writers


@dataclass(frozen=True)
class ReservedLabel(BaseLabel):
    """A key core owns. Plugins can write it and cannot define it."""

    reserved: ClassVar[bool] = True


Validator = Callable[[str], None]  # raises LabelError


class LabelMaker:
    """The registry of label keys. A core service."""

    def __init__(self) -> None:
        self._labels: dict[str, BaseLabel] = {}
        self._config: set[str] = set()  # keys the owner defined in config.toml
        self._core: set[str] = set()  # keys core ships
        self._validators: dict[str, Validator] = {}

    @classmethod
    def core(cls) -> "LabelMaker":
        """A maker holding core's reserved keys."""
        maker = cls()
        for label in (
            ReservedLabel.new("queue", "The queue a job is routed to"),
            ReservedLabel.new("priority", "Urgency", ("urgent", "high", "normal", "low")),
            ReservedLabel.new("confidential", "Keep off remote models", ("true", "false")),
            ReservedLabel.new("format", "How Intake's format step went: ok, none (no format asked), unmatched (no formatter for it), failed (it would not parse)", ("ok", "none", "unmatched", "failed")),
            ReservedLabel.new("triage", "Waiting for a triage stage; the stage that decides clears it", ("pending",)),
            ReservedLabel.new("embed", "Embed this item (else [jobs] embed_by_default)", ("true", "false")),
            ReservedLabel.new("store", "Store this item (else [jobs] store_by_default)", ("true", "false")),
        ):
            maker._labels[label.key] = label
            maker._core.add(label.key)
        return maker

    def register(self, label: BaseLabel) -> None:
        """Define a key. Re-registering the identical definition is allowed (a plugin reload)."""
        have = self._labels.get(label.key)
        if have is not None:
            if have.reserved:
                raise LabelError(f"{label.key!r} is reserved{' by core' if label.key in self._core else ''}: it cannot be defined again")
            if have != label:
                raise LabelError(f"{label.key!r} is already defined differently")
            return
        self._labels[label.key] = label

    def register_config(self, label: BaseLabel) -> None:
        """Define a key from ``[labels.<key>]``. The owner's, so the console may change it."""
        self.register(label)
        self._config.add(label.key)

    def source(self, key: str) -> str:
        """Who defined it: ``core``, ``config`` (the owner) or ``plugin``."""
        self.get(key)
        return "core" if key in self._core else "config" if key in self._config else "plugin"

    def keys(self) -> list[BaseLabel]:
        """Every defined key, by name."""
        return sorted(self._labels.values(), key=lambda b: b.key)

    def set_validator(self, key: str, check: Validator) -> None:
        """Add a check to a registered key, run on every write (``queue`` must name a real queue)."""
        self.get(key)
        self._validators[key] = check

    def get(self, key: str) -> BaseLabel:
        try:
            return self._labels[key]
        except KeyError:
            raise LabelError(f"unknown label key {key!r}") from None

    def check_writer(self, key: str, actor: Actor) -> None:
        if not self.get(key).may_write(actor):
            raise LabelError(f"{key}: only {', '.join(self.get(key).writers)} may write it, not {actor.kind}:{actor.label}")

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
        self.maker.check_writer(key, actor)
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

    async def find(self, key: str, value: str | None = None, limit: int = 100) -> list[tuple[str, str]]:
        """Subjects carrying ``key`` (with ``value``, if given), as (subject, effective value), by subject.
        shortcut: reads every row of one key and sorts out the layers here; do it in SQL when one key holds many thousands."""
        self.maker.get(key)
        rows = await self.db.fetchall("SELECT subject_id, layer, value FROM labels WHERE key = ? ORDER BY subject_id, layer = 'human'", (key,))
        effective = {r["subject_id"]: r["value"] for r in rows}  # human rows sort last, so they win
        return [(sid, v) for sid, v in effective.items() if value is None or v == value][:limit]

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

    async def clear_human_all(self, subject_id: str, actor: Actor) -> int:
        """Remove every human value of a subject (a rejected intake item must leave nothing)."""
        return await self._clear(subject_id, HUMAN, actor, None)

    async def _clear(self, subject_id: str, layer: str, actor: Actor, key: str | None) -> int:
        async with self.db.transaction() as tx:
            sql = "SELECT key, value FROM labels WHERE subject_id = ? AND layer = ?"
            args: list[str] = [subject_id, layer]
            if key is not None:
                sql += " AND key = ?"
                args.append(key)
            rows = await tx.fetchall(sql, args)
            if key is not None:
                self.maker.check_writer(key, actor)
            rows = [r for r in rows if self.maker.get(r["key"]).may_write(actor)]  # a sweep leaves a guarded key alone
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

    async def move(self, src: str, dst: str) -> int:
        """Move every label of ``src`` to ``dst``, both layers, keeping who set each and when
        (intake: labels written on a provisional subject follow the item to its real record).
        One log row for each label, on each side. Returns how many moved."""
        async with self.db.transaction() as tx:
            rows = await tx.fetchall("SELECT * FROM labels WHERE subject_id = ?", (src,))
            for r in rows:
                actor = Actor(r["actor_kind"], r["actor"])  # type: ignore[arg-type]
                await tx.execute(
                    "INSERT INTO labels (subject_id, key, layer, value, actor_kind, actor, at)"
                    " VALUES (?,?,?,?,?,?,?) ON CONFLICT (subject_id, key, layer) DO UPDATE SET"
                    " value = excluded.value, actor_kind = excluded.actor_kind,"
                    " actor = excluded.actor, at = excluded.at",
                    (dst, r["key"], r["layer"], r["value"], r["actor_kind"], r["actor"], r["at"]),
                )
                await tx.execute(
                    "DELETE FROM labels WHERE subject_id = ? AND key = ? AND layer = ?",
                    (src, r["key"], r["layer"]),
                )
                await self.log.append(
                    tx, actor=actor, subject_id=dst, key=r["key"], op="set", layer=r["layer"],
                    new=r["value"],
                )  # fmt: skip
                await self.log.append(
                    tx, actor=actor, subject_id=src, key=r["key"], op="clear", layer=r["layer"],
                    old=r["value"],
                )  # fmt: skip
        return len(rows)
