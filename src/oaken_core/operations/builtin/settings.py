# SPDX-License-Identifier: Apache-2.0
"""Core's own settings, read and changed one section at a time (``settings_edit`` says which keys)."""

import tempfile
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from oaken_core import paths
from oaken_core.config_edit import save_table, set_table
from oaken_core.errors import InvalidInput
from oaken_core.operations.context import OpContext
from oaken_core.operations.registry import Change, Preview, operation
from oaken_core.settings import Settings
from oaken_core.settings_edit import SECTIONS, Section, key_of, section, settings_of, table_path

RELOAD_NOTE = "Saved to config.toml. It reaches the running core on reload (the console's Reload core, or `oaken reload`)."


class SettingRow(BaseModel):
    key: str
    value: Any
    default: Any
    editable: bool
    effect: str
    note: str


class SectionRow(BaseModel):
    name: str
    summary: str
    settings: list[SettingRow]


class SettingsGetIn(BaseModel):
    section: str = Field(default="", description="One section; empty for all of them.")


class SettingsGetOut(BaseModel):
    sections: list[SectionRow]


def rows(sec: Section, current: Settings) -> list[SettingRow]:
    model = settings_of(sec, current)
    values, defaults = model.model_dump(mode="json"), type(model)().model_dump(mode="json")
    out = []
    for key in type(model).model_fields:
        rule = key_of(sec, key)
        out.append(SettingRow(key=key, value=values[key], default=defaults[key], editable=rule.editable, effect=rule.effect, note=rule.note))  # fmt: skip
    return out


@operation("settings_get", "Core's own settings by section: values, defaults, and which can be changed here.",
           permission="read", input=SettingsGetIn, output=SettingsGetOut, mcp="off", group="settings")  # fmt: skip
async def settings_get(ctx: OpContext, inp: SettingsGetIn) -> SettingsGetOut:
    secs = [section(inp.section)] if inp.section else list(SECTIONS)
    return SettingsGetOut(sections=[SectionRow(name=s.name, summary=s.summary, settings=rows(s, ctx.svc.settings)) for s in secs])


class SettingsSetIn(BaseModel):
    section: str
    values: dict[str, Any] = Field(description="The keys to change, with their new values.")


class SettingsSetOut(BaseModel):
    section: str
    changed: list[str]
    effect: str
    note: str


def _config_path() -> Path:
    return paths.config_dir() / "config.toml"


def _plan(ctx: OpContext, inp: SettingsSetIn) -> tuple[Section, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """The section, the whole new table, the keys that actually change, and what the file holds now
    (defaults for keys it does not set; not the running core's values, which a hand edit may not have
    reached yet). Raises ``InvalidInput`` naming the first key that may not be changed here, and when
    the whole file would no longer load."""
    sec = section(inp.section)
    model = settings_of(sec, ctx.svc.settings)
    for key in inp.values:
        if key not in type(model).model_fields:
            raise InvalidInput(f"{key} is not a setting of {sec.name}")
        rule = key_of(sec, key)
        if not rule.editable:
            raise InvalidInput(f"{sec.name}.{key} cannot be changed here ({rule.note})")
    try:
        text = _config_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    existing: Any = tomllib.loads(text) if text else {}
    for part in table_path(sec):
        existing = existing.get(part, {}) if isinstance(existing, dict) else {}
    saved = {**type(model)().model_dump(mode="json"), **existing}
    changed = {k: v for k, v in inp.values.items() if v != saved[k]}
    merged = {**existing, **changed}
    with tempfile.TemporaryDirectory() as tmp:  # the whole file has to load before anything is written
        probe = Path(tmp) / "config.toml"
        probe.write_text(set_table(text, table_path(sec), merged), encoding="utf-8")
        try:
            Settings.load(probe)
        except Exception as exc:  # noqa: BLE001 - ConfigError, with pydantic's field messages
            raise InvalidInput(f"{sec.name}: {getattr(exc, 'message', exc)}".replace(str(probe), "config.toml")) from None
    return sec, merged, changed, saved


async def _preview(ctx: OpContext, inp: SettingsSetIn) -> Preview:
    sec, _merged, changed, saved = _plan(ctx, inp)
    changes = [Change(target=f"{sec.name}.{k}", field=k, before=saved[k], after=v) for k, v in changed.items()]
    return Preview(summary=f"Change {len(changes)} setting(s) in [{sec.name}] of config.toml" if changes else f"[{sec.name}] already holds these values",
                   changes=changes, total_changes=len(changes))  # fmt: skip


@operation("settings_set", "Change keys of one section of core's settings: checked as a whole, then written to config.toml.",
           permission="mutate", input=SettingsSetIn, output=SettingsSetOut, mcp="never", group="settings",
           preview=_preview)  # fmt: skip
async def settings_set(ctx: OpContext, inp: SettingsSetIn) -> SettingsSetOut:
    sec, merged, changed, _saved = _plan(ctx, inp)
    if changed:
        save_table(_config_path(), table_path(sec), merged)
    effects = {key_of(sec, k).effect for k in changed}
    effect = "restart" if "restart" in effects else ("reload" if changed else "none")
    return SettingsSetOut(section=sec.name, changed=sorted(changed), effect=effect,
                          note=RELOAD_NOTE if changed else "Nothing to change.")  # fmt: skip


OPERATIONS = (settings_get, settings_set)
