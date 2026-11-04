# SPDX-License-Identifier: Apache-2.0
"""Which operations an MCP client may call: the owner's setting, inside a ceiling fixed in code.

Adapted from oaken-mail ``core/operations/exposure.py`` (cf3b187); the audit log is the change log.

``Operation.mcp`` is the ceiling and the default: ``never`` cannot be turned on by anything stored
here, ``off`` and ``on`` are the starting point. The owner then turns one operation, or all the
operations a plugin offers, on or off (the console and CLI call ``exposure_set``, which is itself
never offered over MCP, so an agent cannot widen its own reach). Every change is logged. The one way
past ``never`` is the config file's ``lift_never`` list, by name.
"""

import logging
from collections.abc import Collection, Mapping

from oaken_core.actors import Actor
from oaken_core.changelog import ChangeLog
from oaken_core.clock import Clock, fmt_ts
from oaken_core.db import Database
from oaken_core.operations.registry import AnyOperation, OperationRegistry

OP, PLUGIN = "op", "plugin"
CONFIG_ACTOR = "config"  # updated_by marker for rows seeded from the config file, not a person

log = logging.getLogger(__name__)


def scope_of(kind: str, name: str) -> str:
    return f"{kind}:{name}"


class ExposureStore:
    def __init__(
        self, db: Database, clock: Clock, changelog: ChangeLog, lifted: Collection[str] = ()
    ) -> None:
        self.db, self.clock, self.changelog = db, clock, changelog
        self.lifted = frozenset(lifted)  # operations whose ``never`` the config file lifted

    async def settings(self) -> dict[str, bool]:
        rows = await self.db.fetchall("SELECT scope, mcp_enabled FROM operation_settings")
        return {r["scope"]: bool(r["mcp_enabled"]) for r in rows}

    def allowed(self, op: AnyOperation) -> bool:
        """The ceiling: may anything turn this on for MCP? ``never`` unless the config lifted it."""
        return op.mcp_allowed or op.name in self.lifted

    async def seed(self, allowed: Mapping[str, bool], registry: OperationRegistry) -> None:
        """Make the config-derived rows match ``[mcp.allowed]`` exactly: the file is the source of
        truth. A key is an operation name or a plugin name. Unknown names, and ``never`` operations
        (which no setting can expose), are warned about and skipped: a typo in a headless file must
        not stop startup. Idempotent; a key removed from the file drops its row on the next load."""
        ops = {op.name: op for op in registry.all()}
        plugins = {op.plugin for op in registry.all() if op.plugin is not None}
        resolved: dict[str, bool] = {}
        for key, value in allowed.items():
            if key in ops:
                if not self.allowed(ops[key]):
                    log.warning("ignoring [mcp.allowed] %r: that operation is never exposable", key)
                    continue
                resolved[scope_of(OP, key)] = bool(value)
            elif key in plugins:
                resolved[scope_of(PLUGIN, key)] = bool(value)
            else:
                log.warning("ignoring [mcp.allowed] %r: no such operation or plugin", key)
        async with self.db.transaction() as tx:
            await tx.execute("DELETE FROM operation_settings WHERE updated_by = ?", (CONFIG_ACTOR,))
            now = fmt_ts(self.clock.now())
            for scope, enabled in resolved.items():
                await tx.execute(
                    "INSERT INTO operation_settings (scope, mcp_enabled, updated_at, updated_by)"
                    " VALUES (?,?,?,?) ON CONFLICT (scope) DO UPDATE SET"
                    " mcp_enabled = excluded.mcp_enabled, updated_at = excluded.updated_at,"
                    " updated_by = excluded.updated_by",
                    (scope, int(enabled), now, CONFIG_ACTOR),
                )

    def effective(self, op: AnyOperation, chosen: Mapping[str, bool]) -> bool:
        """Is ``op`` offered over MCP? The ceiling first, then the operation's own row, then its
        plugin's row, then its default."""
        if not self.allowed(op):
            return False
        own = chosen.get(scope_of(OP, op.name))
        if own is not None:
            return own
        if op.plugin is not None:
            group = chosen.get(scope_of(PLUGIN, op.plugin))
            if group is not None:
                return group
        return op.mcp_exposed

    async def enabled(self, op: AnyOperation) -> bool:
        return self.effective(op, await self.settings())

    async def set(self, scope: str, enabled: bool | None, *, actor: Actor) -> bool | None:
        """Choose (or, with None, clear the choice for) one scope, and log it in the same
        transaction. Returns the choice that was there before."""
        async with self.db.transaction() as tx:
            row = await tx.fetchone(
                "SELECT mcp_enabled FROM operation_settings WHERE scope = ?", (scope,)
            )
            before = None if row is None else bool(row["mcp_enabled"])
            if enabled is None:
                await tx.execute("DELETE FROM operation_settings WHERE scope = ?", (scope,))
            else:
                await tx.execute(
                    "INSERT INTO operation_settings (scope, mcp_enabled, updated_at, updated_by)"
                    " VALUES (?,?,?,?) ON CONFLICT (scope) DO UPDATE SET"
                    " mcp_enabled = excluded.mcp_enabled, updated_at = excluded.updated_at,"
                    " updated_by = excluded.updated_by",
                    (scope, int(enabled), fmt_ts(self.clock.now()), actor.label or actor.kind),
                )
            await self.changelog.append(
                tx, actor=actor, subject_id=scope, key="exposure", op="set",
                old=None if before is None else str(before).lower(),
                new=None if enabled is None else str(enabled).lower(),
            )  # fmt: skip
        return before
