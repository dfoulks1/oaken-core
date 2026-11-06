# SPDX-License-Identifier: Apache-2.0
"""Core's own settings, read and changed one section at a time (``settings_edit`` says which keys)."""

import os
import shutil
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from oaken_core import paths
from oaken_core.config_edit import refuse_secrets, save_table, set_table
from oaken_core.errors import ConfigError, InvalidInput, NotFound
from oaken_core.labels import BaseLabel, LabelError, LabelMaker
from oaken_core.operations.context import OpContext
from oaken_core.plugins.kinds import check_queue_kind
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


class ConfigText(BaseModel):
    text: str = Field(description="The whole of config.toml.")


class ConfigValidateOut(BaseModel):
    ok: bool
    error: str = ""


def _check_text(ctx: OpContext, text: str) -> None:
    """Raise ``ConfigError`` unless ``text`` is a config core would start on: valid TOML, no secret,
    core's sections sound, and each plugin table fits its plugin's ``config_model``. A plugin that is
    not installed is not an error here (it fails at reload, visibly, and the rest keeps running)."""
    settings = Settings.from_text(text)  # TOML and core's sections
    try:
        refuse_secrets(tomllib.loads(text))
    except ConfigError as exc:
        raise ConfigError(exc.message) from None
    host = ctx.svc.host
    for name, q in settings.queues.items():
        if host is not None:
            check_queue_kind(host.kinds, name, q.kind)
    core_keys = LabelMaker.core()
    for key, lt in settings.labels.items():
        try:
            BaseLabel.new(key, lt.description, tuple(lt.values))
            if key in {b.key for b in core_keys.keys()}:
                raise LabelError(f"{key!r} is reserved by core")
        except LabelError as exc:
            raise ConfigError(f"[labels.{key}]: {exc.message}") from None
    for kind, named in settings.plugins.tables.items():
        for name, table in named.items():
            try:
                problems = host.check_config(name, {k: v for k, v in table.items() if k != "plugin"}) if host else []
            except NotFound:
                continue
            if problems:
                raise ConfigError("; ".join(problems))


@operation("config_validate", "Check the text of a whole config.toml without saving it.",
           permission="read", input=ConfigText, output=ConfigValidateOut, mcp="never", group="config")  # fmt: skip
async def config_validate(ctx: OpContext, inp: ConfigText) -> ConfigValidateOut:
    try:
        _check_text(ctx, inp.text)
    except ConfigError as exc:
        return ConfigValidateOut(ok=False, error=exc.message.replace("config.toml: ", ""))
    return ConfigValidateOut(ok=True)


class ConfigSaveOut(BaseModel):
    saved: bool
    backup: str = ""
    note: str


async def _save_preview(ctx: OpContext, inp: ConfigText) -> Preview:
    return Preview(summary="Replace the whole of config.toml (the old file is kept as config.toml.bak)",
                   changes=[Change(target="config.toml", field="text")], total_changes=1)  # fmt: skip


@operation("config_save", "Replace config.toml with new text: checked first, the old file kept as config.toml.bak.",
           permission="mutate", input=ConfigText, output=ConfigSaveOut, mcp="never", group="config",
           preview=_save_preview)  # fmt: skip
async def config_save(ctx: OpContext, inp: ConfigText) -> ConfigSaveOut:
    _check_text(ctx, inp.text)  # raises: nothing is written for a file core would not start on
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = ""
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))  # one level: the file before this save
        backup = str(path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(inp.text, encoding="utf-8")
    os.replace(tmp, path)
    return ConfigSaveOut(saved=True, backup=backup, note=RELOAD_NOTE)


OPERATIONS = (settings_get, settings_set, config_validate, config_save)
