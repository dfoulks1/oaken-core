# SPDX-License-Identifier: Apache-2.0
"""``log_tail``: the log, in widening scope. One plugin's own lines, one stage's (its jobs, and the plugins that run in it), or
everything. Read from the ``log_lines`` table, or from the log file when ``[log] file`` is set. The lines are as ``logging`` made
them: already scrubbed."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from oaken_core import logstore
from oaken_core.logging import _log_path
from oaken_core.operations.context import OpContext
from oaken_core.operations.registry import operation
from oaken_core.operations.services import need

LEVELS = ("debug", "info", "warning", "error", "critical")


class LogTailIn(BaseModel):
    plugin: str | None = Field(default=None, description="One plugin as kind/name (triager/llm): only its own lines.")
    stage: str | None = Field(default=None, description="One stage, a queue's name: its jobs' lines and those of the plugins that run in it.")
    level: Literal["debug", "info", "warning", "error"] | None = Field(default=None, description="This level and above.")
    limit: int = Field(default=200, ge=1, le=2000)


class LogLine(BaseModel):
    ts: str
    level: str
    logger: str
    plugin: str
    queue: str
    event: str
    data: dict[str, Any]


class LogTailOut(BaseModel):
    kept_in: Literal["table", "file", "off"]
    path: str = ""  # the log file, when it is the log and it exists
    lines: list[LogLine]  # oldest first


def _stage_plugins(ctx: OpContext, stage: str) -> list[str]:
    queue = ctx.svc.queues.get(stage)
    host = need(ctx.svc.host, "the plugin host")
    first = next((q.name for q in ctx.svc.queues.all() if q.kind == queue.kind), None)
    return [f"{p.kind}/{p.name}" for p in host.statuses() if p.kind == queue.kind and (host.tables.table(p.kind, p.name).get("stage") or first) == stage]


@operation("log_tail", "The log, in widening scope: one plugin's lines, one stage's, or everything. Newest last.",
           permission="read", input=LogTailIn, output=LogTailOut, mcp="off", group="log")  # fmt: skip
async def log_tail(ctx: OpContext, inp: LogTailIn) -> LogTailOut:
    cfg = ctx.svc.settings.log
    if not cfg.enabled:
        return LogTailOut(kept_in="off", lines=[])
    near = [lv for lv in LEVELS[LEVELS.index(inp.level):]] if inp.level else list(LEVELS)
    members = _stage_plugins(ctx, inp.stage) if inp.stage else []
    if cfg.file:
        rows = [r for r in logstore.read_file(_log_path(cfg.file), inp.limit)
                if str(r.get("level", "")) in near
                and (not inp.plugin or logstore.plugin_of(str(r.get("logger", ""))) == inp.plugin)
                and (not inp.stage or r.get("queue") == inp.stage or logstore.plugin_of(str(r.get("logger", ""))) in members)]  # fmt: skip
        lines = [LogLine(ts=str(r.get("ts", "")), level=str(r.get("level", "")), logger=str(r.get("logger", "")), plugin=logstore.plugin_of(str(r.get("logger", ""))),
                         queue=str(r.get("queue", "")), event=str(r.get("event", "")),
                         data={k: v for k, v in r.items() if k not in ("ts", "level", "logger", "event", "queue")}) for r in rows[-inp.limit:]]  # fmt: skip
        path = _log_path(cfg.file)
        return LogTailOut(kept_in="file", path=str(path) if path.exists() else "", lines=lines)
    where, args = ["level IN (%s)" % ",".join("?" * len(near))], list(near)
    if inp.plugin:
        where.append("plugin = ?")
        args.append(inp.plugin)
    if inp.stage:
        where.append("(queue = ?" + (" OR plugin IN (%s)" % ",".join("?" * len(members)) if members else "") + ")")
        args += [inp.stage, *members]
    found = await ctx.svc.db.fetchall(f"SELECT ts, level, logger, plugin, queue, event, data FROM log_lines WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", (*args, inp.limit))  # noqa: S608
    import json

    lines = [LogLine(ts=r["ts"], level=r["level"], logger=r["logger"], plugin=r["plugin"], queue=r["queue"], event=r["event"], data=json.loads(r["data"] or "{}")) for r in reversed(found)]
    return LogTailOut(kept_in="table", lines=lines)


OPERATIONS = (log_tail,)
