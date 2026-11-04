# SPDX-License-Identifier: Apache-2.0
"""The change log: one append-only row per change, with who did it.

``append`` is the only writer and runs inside the caller's transaction, so a change and its log row
commit together or not at all. The table refuses update and delete (see migration 0001).
"""

from dataclasses import dataclass

from oaken_core.actors import Actor
from oaken_core.clock import Clock, fmt_ts
from oaken_core.db import Database, Tx
from oaken_core.ids import uuid7


@dataclass(frozen=True)
class LogRow:
    id: str
    ts: str
    actor_kind: str
    actor: str
    subject_id: str
    key: str
    layer: str | None
    op: str
    old_value: str | None
    new_value: str | None


class ChangeLog:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    async def append(
        self,
        tx: Tx,
        *,
        actor: Actor,
        subject_id: str,
        key: str,
        op: str,
        layer: str | None = None,
        old: str | None = None,
        new: str | None = None,
    ) -> str:
        row_id = str(uuid7())
        await tx.execute(
            "INSERT INTO change_log (id, ts, actor_kind, actor, subject_id, key, layer, op,"
            " old_value, new_value) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (row_id, fmt_ts(self.clock.now()), actor.kind, actor.label, subject_id, key, layer,
             op, old, new),
        )  # fmt: skip
        return row_id

    async def rows(
        self, db: Database, subject_id: str | None = None, key: str | None = None, limit: int = 100
    ) -> list[LogRow]:
        where, args = [], []
        if subject_id is not None:
            where.append("subject_id = ?")
            args.append(subject_id)
        if key is not None:
            where.append("key = ?")
            args.append(key)
        sql = "SELECT * FROM change_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq LIMIT ?"
        found = await db.fetchall(sql, (*args, limit))
        return [LogRow(**{k: r[k] for k in LogRow.__dataclass_fields__}) for r in found]
