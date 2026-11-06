# SPDX-License-Identifier: Apache-2.0
"""Labels. A person's write lands in the human layer, an agent's in the machine layer: the caller's
actor decides, never the input. Clearing is human-only (``never`` over MCP)."""

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from oaken_core import paths
from oaken_core.config_edit import get_table, remove_table, set_table
from oaken_core.errors import Conflict, ConfigError, InvalidInput
from oaken_core.labels import BaseLabel, LabelError
from oaken_core.operations.builtin.queue import reload_core
from oaken_core.operations.builtin.settings import _check_text
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


class Empty(BaseModel):
    pass


class LabelKey(BaseModel):
    key: str
    description: str
    values: list[str]  # empty: any text
    reserved: bool  # core's own
    source: Literal["core", "config", "plugin"]  # who defined it; only "config" can be changed here


class LabelKeysOut(BaseModel):
    keys: list[LabelKey]


@operation("label_keys", "Every label key: what it is for, the values it allows, and whether core reserves it.",
           permission="read", input=Empty, output=LabelKeysOut, mcp="on", group="label")  # fmt: skip
async def label_keys(ctx: OpContext, inp: Empty) -> LabelKeysOut:
    return LabelKeysOut(keys=[LabelKey(key=b.key, description=b.description, values=list(b.values), reserved=b.reserved, source=ctx.svc.labels.maker.source(b.key))
                              for b in ctx.svc.labels.maker.keys()])  # fmt: skip


class LabelFindIn(BaseModel):
    key: str
    value: str | None = Field(default=None, description="Only subjects whose effective value is this.")
    limit: int = Field(default=100, ge=1, le=1000)


class LabelHit(BaseModel):
    subject_id: str
    value: str


class LabelFindOut(BaseModel):
    key: str
    hits: list[LabelHit]


@operation("label_find", "Subjects that carry a label key, optionally with one value.",
           permission="read", input=LabelFindIn, output=LabelFindOut, mcp="on", group="label")  # fmt: skip
async def label_find(ctx: OpContext, inp: LabelFindIn) -> LabelFindOut:
    try:
        found = await ctx.svc.labels.find(inp.key, inp.value, inp.limit)
    except LabelError as exc:
        raise InvalidInput(exc.message) from None
    return LabelFindOut(key=inp.key, hits=[LabelHit(subject_id=s, value=v) for s, v in found])


# -- define, change and remove a key (the owner's, in config.toml) -----------------------------
class LabelNewIn(BaseModel):
    key: str = Field(description="Lower case, starting with a letter: a-z, 0-9, _ and . only.")
    description: str = ""
    values: list[str] = Field(default_factory=list, description="The values it allows. Empty: any text.")


class LabelEditIn(BaseModel):
    key: str = Field(description="The key. It stays; delete and add to change it.")
    description: str | None = None
    values: list[str] | None = Field(default=None, description="Replaces the allowed values. Empty: any text. Values already set are not rechecked.")


class LabelKeyRef(BaseModel):
    key: str


class LabelChangeOut(BaseModel):
    key: str
    reloading: bool
    note: str


def _path() -> Path:
    return paths.config_dir() / "config.toml"


def _text() -> str:
    return _path().read_text(encoding="utf-8") if _path().exists() else ""


def _write(ctx: OpContext, text: str) -> None:
    """Check the whole file would load, then replace it atomically."""
    try:
        _check_text(ctx, text)
    except ConfigError as exc:
        raise InvalidInput(exc.message.replace("config.toml: ", "")) from None
    tmp = _path().with_name(_path().name + f".{os.getpid()}.tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, _path())


def _table(inp: LabelNewIn | LabelEditIn, base: dict[str, Any] | None = None) -> dict[str, Any]:
    t = dict(base or {})
    if inp.description is not None:
        t["description"] = inp.description.strip()
    if inp.values is not None:
        t["values"] = [v.strip() for v in inp.values if v.strip()]
    return {k: v for k, v in t.items() if v}  # an empty description or list is the default: leave it out


def _own(ctx: OpContext, key: str) -> dict[str, Any]:
    """The key's table in config.toml. ``Conflict`` for a key core or a plugin defines, which is not the owner's to change."""
    maker = ctx.svc.labels.maker
    try:
        source = maker.source(key)
    except LabelError as exc:
        raise InvalidInput(exc.message) from None
    if source != "config":
        raise Conflict(f"label {key!r} is defined by {'core' if source == 'core' else 'a plugin'}, not in config.toml")
    table = get_table(_text(), ["labels", key])
    if not table:
        raise Conflict(f"label {key!r} is not in config.toml")
    return table


def _new_plan(ctx: OpContext, inp: LabelNewIn) -> tuple[str, dict[str, Any]]:
    try:
        BaseLabel.new(inp.key, inp.description, tuple(inp.values))
    except LabelError as exc:
        raise InvalidInput(exc.message) from None
    if inp.key in {b.key for b in ctx.svc.labels.maker.keys()}:
        raise Conflict(f"label {inp.key!r} already exists")
    table = _table(inp)
    return set_table(_text(), ["labels", inp.key], table), table


async def preview_label_new(ctx: OpContext, inp: LabelNewIn) -> Preview:
    _text_, table = _new_plan(ctx, inp)
    return Preview(summary=f"Define label {inp.key} and reload core",
                   changes=[Change(target=inp.key, field="label", after=table or "any text")], total_changes=1)  # fmt: skip


@operation("label_new", "Define a label key in config.toml, then core reloads.",
           permission="mutate", input=LabelNewIn, output=LabelChangeOut, mcp="never", group="label",
           preview=preview_label_new)  # fmt: skip
async def label_new(ctx: OpContext, inp: LabelNewIn) -> LabelChangeOut:
    text, _table_ = _new_plan(ctx, inp)
    _write(ctx, text)
    reloading, note = reload_core(ctx, "the key shows in a moment")
    return LabelChangeOut(key=inp.key, reloading=reloading, note=note)


def _edit_plan(ctx: OpContext, inp: LabelEditIn) -> tuple[str, dict[str, Any], dict[str, Any]]:
    before = _own(ctx, inp.key)
    after = _table(inp, before)
    if after == before:
        raise InvalidInput("nothing to change")
    return set_table(_text(), ["labels", inp.key], after), before, after


async def preview_label_edit(ctx: OpContext, inp: LabelEditIn) -> Preview:
    _t, before, after = _edit_plan(ctx, inp)
    changes = [Change(target=inp.key, field=k, before=before.get(k), after=after.get(k))
               for k in sorted(before.keys() | after.keys()) if before.get(k) != after.get(k)]  # fmt: skip
    return Preview(summary=f"Change label {inp.key} and reload core", changes=changes, total_changes=len(changes))


@operation("label_edit", "Change a label key's description or allowed values. Written to config.toml, then core reloads.",
           permission="mutate", input=LabelEditIn, output=LabelChangeOut, mcp="never", group="label",
           preview=preview_label_edit)  # fmt: skip
async def label_edit(ctx: OpContext, inp: LabelEditIn) -> LabelChangeOut:
    text, _b, _a = _edit_plan(ctx, inp)
    _write(ctx, text)
    reloading, note = reload_core(ctx, "the change shows in a moment")
    return LabelChangeOut(key=inp.key, reloading=reloading, note=note)


async def _delete_plan(ctx: OpContext, inp: LabelKeyRef) -> str:
    _own(ctx, inp.key)
    if await ctx.svc.labels.find(inp.key, limit=1):
        raise Conflict(f"subjects still carry {inp.key!r}: find them (label_find) and clear it first")
    return remove_table(_text(), ["labels", inp.key])


async def preview_label_delete(ctx: OpContext, inp: LabelKeyRef) -> Preview:
    await _delete_plan(ctx, inp)
    return Preview(summary=f"Remove label {inp.key} from config.toml and reload core",
                   changes=[Change(target=inp.key, field="label", before=inp.key)], total_changes=1)  # fmt: skip


@operation("label_delete", "Remove a label key from config.toml, then core reloads. Refused while any subject carries it.",
           permission="mutate", input=LabelKeyRef, output=LabelChangeOut, mcp="never", group="label",
           preview=preview_label_delete)  # fmt: skip
async def label_delete(ctx: OpContext, inp: LabelKeyRef) -> LabelChangeOut:
    _write(ctx, await _delete_plan(ctx, inp))
    reloading, note = reload_core(ctx, "the key leaves the list in a moment")
    return LabelChangeOut(key=inp.key, reloading=reloading, note=note)


OPERATIONS = (label_get, label_set, label_clear, label_keys, label_find, label_new, label_edit, label_delete)
