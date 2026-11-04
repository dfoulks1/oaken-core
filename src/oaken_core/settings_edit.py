# SPDX-License-Identifier: Apache-2.0
"""Which of core's own settings a client may edit, and what a change to each one costs.

The settings operations (``get_settings``, ``check_settings``, ``set_settings``) read and write
``config.toml`` one table at a time, like the plugin settings do, but only for the keys listed here.
This module is that list, in one place. Anything that changes where data lives, which port or
address core listens on, or touches security (``[api]``, ``[storage]``, token files, the model and
plugin choices) is not on it, and a client cannot change it from here.

``Effect`` says how a saved change reaches the running core:

* ``reload``  - core re-reads its configuration in place (``oaken reload``); no restart.
* ``reindex`` - a reload applies it to new mail; mail already in the index follows only after
  ``oaken rebuild-index``.
* ``restart`` - a reload cannot apply it; core has to be restarted. No key listed today needs this
  (a reload rebuilds everything but the listening socket, and reconfigures logging), but the
  mechanism is here, tested, so the day a key does, it is reported honestly.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from oaken_core.errors import NotFound

Effect = Literal["reload", "reindex", "restart"]


@dataclass(frozen=True)
class Key:
    effect: Effect = "reload"
    editable: bool = True  # False: shown with its value, changed only by hand in config.toml
    note: str = ""


@dataclass(frozen=True)
class Section:
    name: str  # what a client calls it
    table: str  # the table in config.toml, and the attribute of ``Settings``
    summary: str
    keys: Mapping[str, Key] = field(default_factory=dict)


def _keys(*names: str, effect: Effect = "reload") -> dict[str, Key]:
    return {n: Key(effect) for n in names}


SECTIONS: tuple[Section, ...] = (
    Section(
        "index",
        "index",
        "The search index: threading by subject, and the limits on a search query.",
        _keys("subject_join_days", "max_query_chars", "max_query_terms"),
    ),
    Section(
        "retrieval",
        "search",
        "Search: how keyword and vector results are merged.",
        _keys(
            "rrf_k",
            "keyword_weight",
            "vector_weight",
            "keyword_min_match",
            "exact_boost",
            "overfetch",
            "max_candidates",
            "vector_timeout_seconds",
        ),
    ),
    Section(
        "read_state",
        "read_state",
        "Marking mail read: automatic marking, and how audit rows are batched.",
        _keys("auto_mark", "audit_chunk"),
    ),
    Section(
        "ui",
        "ui",
        "Reading in a client, and how core's console pages look.",
        _keys("mark_read_delay_ms", "palette", "mode"),
    ),
    Section(
        "sync",
        "sync",
        "Sync defaults. The minimum interval is a floor and is set by hand.",
        {
            **_keys(
                "interval_seconds",
                "timezone",
                "max_concurrent",
                "backoff_max_seconds",
                "backoff_jitter",
                "batch_size",
            ),
            "min_interval_seconds": Key(editable=False, note="a floor; edit config.toml by hand"),
        },
    ),
    Section(
        "ai",
        "ai",
        "Token thrift: how much of a message a model reads, and how chunks are cut. The models "
        "and backends are plugin settings.",
        {
            **_keys(
                "enrich_max_input_tokens",
                "triage_gate",
                "background_yield_seconds",
                "max_output_chars",
            ),
            **_keys("chunk_tokens", "chunk_overlap", effect="reindex"),
        },
    ),
    Section(
        "ingest",
        "ingest",
        "How ingest treats mail: what triage must rate before mail is embedded, and how many "
        "times a stage retries.",
        _keys("embed_min_priority", "stage_max_attempts", "backfill_queue_cap"),
    ),
    Section(
        "attachments",
        "mail.attachments",
        "Where attachments are kept. Core moves every one out of the message into this folder when "
        "mail is stored. Whether quarantine is on is set by hand in config.toml.",
        _keys("quarantine_dir"),
    ),
    Section(
        "mcp",
        "mcp",
        "Whether MCP is on at all, and which workspaces a client may reach. Which operations it may call is set per "
        "operation (`oaken operations`).",
        {
            "enabled": Key(note="the master switch; the door itself is `[api] mcp`"),
            "workspaces": Key(),
            "allowed": Key(editable=False, note="set with `oaken operations enable|disable`"),
        },
    ),
    Section(
        "log",
        "log",
        "What core logs. The log file's place and rotation are set by hand.",
        _keys("level", "format"),
    ),
)

_BY_NAME = {s.name: s for s in SECTIONS} | {
    "search": next(s for s in SECTIONS if s.table == "search")
}


def section(name: str) -> Section:
    """The section called ``name`` (``search`` is accepted for ``retrieval``)."""
    found = _BY_NAME.get(name)
    if found is None:
        raise NotFound(f"no settings section {name!r} (there are: {', '.join(known())})")
    return found


def table_path(sec: Section) -> list[str]:
    """The table's place in config.toml: ``["mail", "attachments"]`` for ``mail.attachments``."""
    return sec.table.split(".")


def settings_of(sec: Section, current: object) -> BaseModel:
    """The settings model of a section, found by walking ``mail.attachments`` down from ``Settings``."""
    for part in table_path(sec):
        current = getattr(current, part)
    if not isinstance(current, BaseModel):
        raise TypeError(f"{sec.table} is not a settings model")
    return current


def known() -> list[str]:
    return [s.name for s in SECTIONS]


def env_var(sec: Section, key: str) -> str:
    """The environment variable that overrides ``key`` (``OAKEN_AI__CHUNK_TOKENS``)."""
    return f"OAKEN_{sec.table.replace('.', '__')}__{key}".upper()


def env_overrides(sec: Section, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """``{key: variable}`` for each of the section's keys that the environment overrides. The names
    are matched without regard to case, as the settings loader matches them."""
    env = {k.upper() for k in (os.environ if environ is None else environ)}
    return {k: v for k in sec.keys if (v := env_var(sec, k)) in env}
