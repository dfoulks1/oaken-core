# SPDX-License-Identifier: Apache-2.0
"""Which of core's own settings a client may edit, and what a change to each one costs.

Lifted in shape from oaken-mail's ``settings_edit.py`` (cf3b187), with core's sections. The settings
operations (``settings_get``, ``settings_set``) read and write ``config.toml`` one table at a time, like
the plugin settings do, but only for the keys listed here as editable. Anything that opts an outside
worker in, widens who may see confidential text, or changes where core listens or keeps data is shown
and set by hand in ``config.toml``. A key a section does not list is treated the same way.

``Effect`` says how a saved change reaches the running core:

* ``reload``  - core re-reads its configuration in place (``oaken reload``, or the console's Reload
  core); no restart.
* ``restart`` - a reload cannot apply it; core has to be restarted. No key listed today needs this, but
  the mechanism is here so the day one does, it is reported honestly.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from oaken_core.errors import NotFound

Effect = Literal["reload", "restart"]
BY_HAND = "set by hand in config.toml"


@dataclass(frozen=True)
class Key:
    effect: Effect = "reload"
    editable: bool = True  # False: shown with its value, changed only by hand in config.toml
    note: str = ""


@dataclass(frozen=True)
class Section:
    name: str  # what a client calls it, and the table in config.toml
    summary: str
    keys: Mapping[str, Key] = field(default_factory=dict)

    @property
    def table(self) -> str:
        return self.name


def _keys(*names: str) -> dict[str, Key]:
    return {n: Key() for n in names}


def _by_hand(*names: str, why: str = "") -> dict[str, Key]:
    return {n: Key(editable=False, note=f"{BY_HAND}{': ' + why if why else ''}") for n in names}


SECTIONS: tuple[Section, ...] = (
    Section(
        "log",
        "What core logs. The log file's place and rotation are set by hand.",
        {**_keys("level", "levels", "format"), **_by_hand("file", "file_max_bytes", "file_backups", "events_file")},
    ),
    Section(
        "jobs",
        "How jobs run: leases, retries, backoff, timeouts and per-kind limits.",
        _keys(
            "lease_seconds", "max_attempts", "backoff_base_seconds", "backoff_max_seconds", "backoff_jitter",
            "handler_timeout_seconds", "max_result_bytes", "kind_limits", "requeue_cooldown_seconds",
            "open_gate_on_final_failure",
        ),
    ),  # fmt: skip
    Section(
        "swarm",
        "External workers (agents) that lease jobs. Turning them on, and which kinds they may take, hand "
        "job text to outside models, so those two are set by hand.",
        {
            **_keys("max_agents", "default_max_leases", "default_max_per_hour", "agent_lease_seconds"),
            **_by_hand("enabled", "allowed_kinds", why="it hands job text to an outside worker"),
        },
    ),
    Section(
        "router",
        "The model router: shortcuts, confidence, token limits and waits. Which LAN backends may see "
        "confidential text is set by hand.",
        {
            **_keys(
                "embedder", "vector_store", "shortcuts", "near_duplicate_score", "junk_index",
                "nearjunk_score", "min_confidence", "default_input_tokens", "default_output_tokens",
                "call_timeout_seconds", "wait_seconds",
            ),
            **_by_hand("lan_allowlist", why="it widens who may see confidential text"),
        },
    ),  # fmt: skip
    Section(
        "console",
        "How the console looks. It always binds 127.0.0.1; its port is set by hand.",
        {**_keys("palette", "mode"), **_by_hand("port")},
    ),
)

_BY_NAME = {s.name: s for s in SECTIONS}


def section(name: str) -> Section:
    found = _BY_NAME.get(name)
    if found is None:
        raise NotFound(f"no settings section {name!r} (there are: {', '.join(known())})")
    return found


def table_path(sec: Section) -> list[str]:
    """The table's place in config.toml."""
    return sec.table.split(".")


def settings_of(sec: Section, current: object) -> BaseModel:
    """The settings model of a section, found by name on ``Settings``."""
    for part in table_path(sec):
        current = getattr(current, part)
    if not isinstance(current, BaseModel):
        raise TypeError(f"{sec.table} is not a settings model")
    return current


def known() -> list[str]:
    return [s.name for s in SECTIONS]


def key_of(sec: Section, name: str) -> Key:
    """The rule for ``name``; a key the section does not list is by hand."""
    return sec.keys.get(name) or Key(editable=False, note=BY_HAND)
