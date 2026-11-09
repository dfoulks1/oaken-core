# SPDX-License-Identifier: Apache-2.0
"""The front door as an operation: put an item into intake (triage, then the ingestor). Without this only a plugin
could submit (``ctx.submit``), so a person at the CLI, an API client and a skin all had no way in."""

from typing import Any

from pydantic import BaseModel, Field

from oaken_core.errors import NotFound, PluginError, Unavailable
from oaken_core.jobs import PRIORITY_USER
from oaken_core.operations.context import OpContext
from oaken_core.operations.registry import Change, Preview, operation
from oaken_core.operations.services import need


class IntakeSubmitIn(BaseModel):
    source: str = Field(min_length=1, max_length=120, description="Where it came from, for the record: a name, never a secret.")
    body: str = Field(min_length=1, description="The item: text. Triagers read it; the ingestor stores it.")
    labels: dict[str, str] = Field(default_factory=dict, description="Labels set before triage, for example confidential=true.")
    format: str | None = Field(default=None, description="What the body is (json, ini, ...): a formatter of that name splits it into parts before triage.")


class IntakeSubmitOut(BaseModel):
    subject_id: str  # provisional: the real one is given when an ingestor stores it
    note: str


async def _preview(ctx: OpContext, inp: IntakeSubmitIn) -> Preview:
    return Preview(summary=f"Submit an item from {inp.source!r} ({len(inp.body):,} characters) to intake for triage",
                   changes=[Change(target="intake", field="submit", after=inp.source)], total_changes=1)  # fmt: skip


@operation("intake_submit", "Put an item into intake: triage decides, then the ingestor stores it. Returns its provisional id.",
           permission="mutate", input=IntakeSubmitIn, output=IntakeSubmitOut, mcp="off", group="intake",
           preview=_preview)  # fmt: skip
async def intake_submit(ctx: OpContext, inp: IntakeSubmitIn) -> IntakeSubmitOut:
    subject = await need(ctx.svc.intake, "intake").submit(inp.source, inp.body, labels=inp.labels, format=inp.format, actor=ctx.caller.actor)
    return IntakeSubmitOut(subject_id=str(subject), note="queued in Intake (default): it is split into parts, then triaged")


class ItemRerunIn(BaseModel):
    id: str = Field(description="The stored message's subject id.")
    stage: str = Field(description="enrich or act. The stages between are skipped.")
    routes: list[str] = Field(default_factory=list, description="act only: the routes whose actions run again.")
    source: str = Field(default="rerun", description="The message's source, for enrichers that read it.")
    priority: int = Field(default=PRIORITY_USER, ge=0, le=100, description="Queue priority. The default is the user's own: it runs next, and runs even if the queue is paused.")


class ItemRerunOut(BaseModel):
    subject_id: str
    note: str


async def _preview_rerun(ctx: OpContext, inp: ItemRerunIn) -> Preview:
    return Preview(summary=f"Run {inp.id} through {inp.stage} again, skipping the stages before it",
                   changes=[Change(target=inp.id, field="rerun", after=inp.stage)], total_changes=1)  # fmt: skip


@operation("item_rerun", "Send a stored message back to one later stage (enrich or act). Intake and triage are skipped.",
           permission="mutate", input=ItemRerunIn, output=ItemRerunOut, mcp="never", group="item", preview=_preview_rerun)  # fmt: skip
async def item_rerun(ctx: OpContext, inp: ItemRerunIn) -> ItemRerunOut:
    # In-process, so no envelope round trip: the operation is the gate (confirm, audit) and calls intake's rerun directly.
    await need(ctx.svc.intake, "intake").rerun(inp.id, {"stage": inp.stage, "routes": inp.routes, "source": inp.source, "priority": inp.priority})
    return ItemRerunOut(subject_id=inp.id, note=f"queued in {inp.stage}")


class ItemShowIn(BaseModel):
    id: str = Field(description="The item's subject id (``oaken items list`` shows them).")
    part: str | None = Field(default=None, description="Only this part (headers, text, body...). Empty: every part.")


class HistoryRow(BaseModel):
    ts: str
    actor: str
    key: str
    op: str
    layer: str | None = None
    value: str | None = None


class ItemShowOut(BaseModel):
    subject_id: str
    ingestor: str  # which ingestor answered
    parts: dict[str, Any]  # what it stored of the item, by part name
    labels: dict[str, str]
    history: list[HistoryRow]  # the change log for it, oldest first


def _ingestor(ctx: OpContext) -> Any:
    """The active ingestor, if it can read items back."""
    from oaken_core.plugins.base import is_implemented

    host = need(ctx.svc.host, "the plugin host")
    try:
        ingestor = host.get("ingestor")
    except PluginError as exc:
        raise Unavailable(f"no ingestor is running ({exc})") from None
    if not is_implemented(ingestor, "read"):
        raise Unavailable("the active ingestor cannot read items back")
    return ingestor


@operation("item_show", "An item's context: the parts the ingestor stored, its labels and its history. Read back through the active ingestor.",
           permission="read", input=ItemShowIn, output=ItemShowOut, mcp="off", group="item")  # fmt: skip
async def item_show(ctx: OpContext, inp: ItemShowIn) -> ItemShowOut:
    ingestor = _ingestor(ctx)
    parts = await ingestor.read(inp.id, inp.part)
    rows = await ctx.svc.changelog.rows(ctx.svc.db, inp.id, None, 50)
    history = [HistoryRow(ts=str(r.ts), actor=f"{r.actor_kind}:{r.actor}" if r.actor else r.actor_kind, key=r.key, op=r.op, layer=r.layer, value=r.new_value) for r in rows]
    return ItemShowOut(subject_id=inp.id, ingestor=type(ingestor).__name__, parts=parts, labels=await ctx.svc.labels.all(inp.id), history=history)


class ItemGetIn(BaseModel):
    id: str = Field(description="The item's subject id.")
    name: str = Field(json_schema_extra={"positional": True},  # positional only on the command line: `oaken item get text --id ID`; over the API it is a field like any other
                      description="What to get: a part (text, headers, body), a path into it (headers.subject, attachment-1.filename, tags.0), or labels.KEY.")  # fmt: skip


class ItemGetOut(BaseModel):
    subject_id: str
    name: str
    value: Any
    text: str  # the value as plain text: a string as it is, anything else as JSON. What the command prints.


def _walk(value: Any, path: list[str], where: str) -> Any:
    for key in path:
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        else:
            has = f" (it has: {', '.join(str(k) for k in value)})" if isinstance(value, dict) else ""
            raise NotFound(f"{where} has no {key!r}{has}")
    return value


@operation("item_get", "One named thing from an item, as a value: a part, a path into a part (headers.subject), or a label (labels.format). Prints just the value.",
           permission="read", input=ItemGetIn, output=ItemGetOut, mcp="off", group="item")  # fmt: skip
async def item_get(ctx: OpContext, inp: ItemGetIn) -> ItemGetOut:
    head, *path = inp.name.split(".")
    if head == "labels":
        labels = await ctx.svc.labels.all(inp.id)
        value: Any = _walk(labels, path, f"item {inp.id!r} labels")
    else:
        found = await _ingestor(ctx).read(inp.id, head)
        value = _walk(found[head], path, f"part {head!r}")
    import json

    return ItemGetOut(subject_id=inp.id, name=inp.name, value=value, text=value if isinstance(value, str) else json.dumps(value, indent=2, default=str))


OPERATIONS = (intake_submit, item_rerun, item_show, item_get)
