# SPDX-License-Identifier: Apache-2.0
"""Labels. A person's write lands in the human layer, an agent's in the machine layer: the caller's
actor decides, never the input. Clearing is human-only (``never`` over MCP)."""

from typing import Literal

from pydantic import BaseModel, Field

from oaken_core.errors import InvalidInput
from oaken_core.operations.context import OpContext
from oaken_core.operations.registry import Change, Preview, operation


class LabelGetIn(BaseModel):
    subject_id: str
    key: str | None = Field(default=None, description="One key; default every label of the subject.")


class LabelValue(BaseModel):
    key: str
    effective: str | None
    human: str | None = None
    machine: str | None = None


class LabelGetOut(BaseModel):
    subject_id: str
    labels: list[LabelValue]


@operation("label_get", "A subject's labels: the effective value and both layers.", permission="read",
           input=LabelGetIn, output=LabelGetOut, mcp="on", group="label")  # fmt: skip
async def label_get(ctx: OpContext, inp: LabelGetIn) -> LabelGetOut:
    store = ctx.svc.labels
    keys = [inp.key] if inp.key else sorted(await store.all(inp.subject_id))
    out = []
    for key in keys:
        layers = await store.layers(inp.subject_id, key)
        out.append(LabelValue(key=key, effective=await store.get(inp.subject_id, key),
                              human=layers.get("human"), machine=layers.get("machine")))  # fmt: skip
    return LabelGetOut(subject_id=inp.subject_id, labels=out)


class LabelSetIn(BaseModel):
    subject_id: str
    key: str
    value: str


class LabelSetOut(BaseModel):
    changed: bool
    layer: Literal["human", "machine"]


async def preview_label_set(ctx: OpContext, inp: LabelSetIn) -> Preview:
    ctx.svc.labels.maker.check(inp.key, inp.value)  # a bad key or value fails before the proposal
    layer = "human" if ctx.caller.actor.kind == "user" else "machine"
    before = (await ctx.svc.labels.layers(inp.subject_id, inp.key)).get(layer)
    return Preview(summary=f"Set {inp.key}={inp.value} on {inp.subject_id} ({layer} layer)",
                   changes=[Change(target=inp.subject_id, field=inp.key, before=before, after=inp.value)],
                   total_changes=1)  # fmt: skip


@operation("label_set", "Set a label. A person's value is not overwritten by a machine's.",
           permission="mutate", input=LabelSetIn, output=LabelSetOut, mcp="off", group="label",
           preview=preview_label_set, audit="service")  # fmt: skip
async def label_set(ctx: OpContext, inp: LabelSetIn) -> LabelSetOut:
    changed = await ctx.svc.labels.set(inp.subject_id, inp.key, inp.value, ctx.caller.actor)
    return LabelSetOut(changed=changed, layer="human" if ctx.caller.actor.kind == "user" else "machine")


class LabelClearIn(BaseModel):
    subject_id: str
    key: str
    layer: Literal["human", "machine"] = "human"


class LabelClearOut(BaseModel):
    cleared: int


async def preview_label_clear(ctx: OpContext, inp: LabelClearIn) -> Preview:
    before = (await ctx.svc.labels.layers(inp.subject_id, inp.key)).get(inp.layer)
    return Preview(summary=f"Clear the {inp.layer} value of {inp.key} on {inp.subject_id}",
                   changes=[Change(target=inp.subject_id, field=inp.key, before=before)],
                   total_changes=1 if before is not None else 0)  # fmt: skip


@operation("label_clear", "Remove one layer of a label. Clearing the human layer is the unlock.",
           permission="mutate", input=LabelClearIn, output=LabelClearOut, mcp="never", group="label",
           preview=preview_label_clear, audit="service")  # fmt: skip
async def label_clear(ctx: OpContext, inp: LabelClearIn) -> LabelClearOut:
    ctx.svc.labels.maker.get(inp.key)
    if inp.layer == "human":
        return LabelClearOut(cleared=int(await ctx.svc.labels.clear_human(inp.subject_id, inp.key, ctx.caller.actor)))
    if ctx.caller.actor.kind == "user":
        return LabelClearOut(cleared=await ctx.svc.labels.clear_machine(inp.subject_id, ctx.caller.actor, inp.key))
    raise InvalidInput("only a person clears labels")


OPERATIONS = (label_get, label_set, label_clear)
