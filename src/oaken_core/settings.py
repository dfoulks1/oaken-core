# SPDX-License-Identifier: Apache-2.0
"""Core's own settings. Written new: oaken-mail's ``settings.py`` (696 lines) is mail-shaped.

Only what core needs today. Products add their sections through the plugin ``config_model``.
"""

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from oaken_core import paths
from oaken_core.errors import ConfigError

Level = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class LogSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True  # the kept log (the file if ``file`` is set, else the ``log_lines`` table); stderr is unaffected
    keep_days: int = Field(default=7, ge=1, le=3650)  # how long the table keeps a line
    level: Level = "INFO"  # for everything under ``oaken_core``
    # Per-logger overrides by dotted name: {"oaken_core.jobs": "DEBUG"}. Python logging resolves
    # the most specific name, so one noisy module can be turned up without the rest.
    levels: dict[str, Level] = Field(default_factory=dict)
    format: Literal["console", "json"] = "console"
    file: str = (
        ""  # also log to this file as JSON; relative paths sit under the data directory
    )
    file_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    file_backups: int = Field(default=5, ge=0, le=100)
    trace: bool = False  # record what models are told and answer, full text, to trace.jsonl (private: off by default)
    events_file: str = "events.jsonl"  # every event, one JSON line each (``events tail``); empty turns it off

    @field_validator("level", mode="before")
    @classmethod
    def _upper(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v

    @field_validator("levels", mode="before")
    @classmethod
    def _upper_all(cls, v: object) -> object:
        return (
            {k: s.upper() if isinstance(s, str) else s for k, s in v.items()}
            if isinstance(v, dict)
            else v
        )


class JobsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_seconds: float = Field(default=60.0, gt=0)
    max_attempts: int = Field(default=3, ge=1)
    backoff_base_seconds: float = Field(default=5.0, ge=0)
    backoff_max_seconds: float = Field(default=900.0, ge=0)
    backoff_jitter: float = Field(
        default=0.2, ge=0, le=1
    )  # a fraction taken off, at random
    handler_timeout_seconds: float = Field(
        default=600.0, ge=0
    )  # 0 turns the timeout off
    max_result_bytes: int = Field(default=65536, ge=1)
    kind_limits: dict[str, int] = Field(
        default_factory=dict
    )  # concurrency cap per job kind
    requeue_cooldown_seconds: float = Field(
        default=60.0, ge=0
    )  # per job, between requeues
    # Intake: is an item that passed triage stored when nothing labelled it ``store``? The ``store``
    # label (true/false), set by a provider, a rule or a triager, decides first. A reject is not stored
    # unless it carries ``store=true`` (a mailbox that keeps its junk labels it so).
    store_by_default: bool = True
    # Intake's format step. ``unmatched``: a format was asked for and no formatter handles it (maybe there is none yet).
    # ``failed``: a formatter was asked and could not parse the item. Each is ``pass`` (go on to triage as one whole ``body``
    # part, labelled ``format``) or ``fail`` (stop in Intake's failed jobs, payload kept, to requeue once a formatter exists).
    unmatched_format: Literal["pass", "fail"] = "pass"
    failed_format: Literal["pass", "fail"] = "pass"
    # Intake: is a stored item embedded when nothing labelled it ``embed``? Off: an embedding is a copy
    # of the text's meaning that a model has seen, so it is the owner's switch (like ``router.junk_index``).
    embed_by_default: bool = False
    # Intake: when a triager fails on the last attempt, treat the item as "unsure" (see ``unsure``).
    # Off by default: a broken triager lets intake back up.
    open_gate_on_final_failure: bool = False


class SwarmSettings(BaseModel):
    """Optional external workers (agents) that lease jobs over the API, the CLI or MCP.

    Off by default. A kind of job is never offered to an agent unless it is in ``allowed_kinds``:
    handing a job to an external model sends it that job's text, so the user opts in kind by kind.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    allowed_kinds: list[str] = Field(default_factory=list)
    max_agents: int = Field(default=8, ge=1, le=256)  # active (unrevoked) agents
    default_max_leases: int = Field(default=2, ge=1)  # jobs one agent may hold at once
    default_max_per_hour: int = Field(
        default=600, ge=1
    )  # jobs one agent may claim per hour
    # How long an agent holds a job before it must heartbeat. Long: a large model on modest
    # hardware is slow.
    agent_lease_seconds: float = Field(default=600.0, gt=0)


class PluginTables(BaseModel):
    """The ``[plugins]`` part of the config: a table per plugin, ``[plugins.<kind>.<name>]``, and
    which plugin is active for a kind that takes exactly one (``active``).

    A table that carries ``plugin = "<entry point>"`` is a *named instance* of that plugin: any
    number of them, each with its own settings (``[plugins.llm_backend.desk]`` and
    ``[plugins.llm_backend.laptop]`` both with the same ``plugin``).
    """

    model_config = ConfigDict(extra="forbid")

    active: dict[str, str] = Field(
        default_factory=dict
    )  # kind -> plugin, e.g. {"store": "sqlite"}
    tables: dict[str, dict[str, dict[str, object]]] = Field(
        default_factory=dict
    )  # kind -> name -> table

    def table(self, kind: str, name: str) -> dict[str, object]:
        return dict(self.tables.get(kind, {}).get(name, {}))

    def instances(self, kind: str) -> dict[str, str]:
        """Instance name -> the plugin it is an instance of, for tables with a ``plugin`` key."""
        return {
            n: str(t["plugin"])
            for n, t in self.tables.get(kind, {}).items()
            if "plugin" in t
        }


class OperationsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_ttl_seconds: float = Field(
        default=300.0, gt=0
    )  # how long a confirmation token lives
    max_pending_confirmations: int = Field(default=64, ge=1)
    proposal_changes_shown: int = Field(
        default=20, ge=1
    )  # changes listed in a proposal
    # ``[mcp.allowed]``: operations (or ``plugin:<name>``) the config turns on or off for MCP. The
    # ``never`` ceiling is not touched by these; only ``lift_never`` below lifts one.
    mcp_allowed: dict[str, bool] = Field(default_factory=dict)
    # Operations whose ``mcp="never"`` ceiling the config file lifts, by name, to ``off`` (then an
    # exposure setting or an agent grant can turn them on). Logged at start. Only a person who can
    # edit the file can do this; no operation can.
    lift_never: list[str] = Field(default_factory=list)
    # DANGER: lifts every ``never`` ceiling at once (each operation then starts off, and the owner turns on what they want). Set from the
    # console's Capabilities page or by hand; no operation can set it, so an agent cannot widen its own reach.
    override_never: bool = False


class QueueTable(BaseModel):
    """``[queues.<name>]``: a queue, which is a pipeline stage for one kind of analyzer plugin."""

    model_config = ConfigDict(extra="forbid")

    kind: str = ""  # an analyzer plugin kind (``triager``, ``enricher``, ``action``, or a product's own); empty only for ``default``
    label: str = ""  # the console's title for it; the name, capitalised, when empty
    description: str = ""
    workers: int = Field(default=1, ge=1, le=64)
    disabled: list[str] = Field(
        default_factory=list
    )  # plugins (by name) switched off in this stage only


class ExtensionsSettings(BaseModel):
    """``[extensions]``: directories of plugin directories (each holds ``oaken-<kind>-<name>/pyproject.toml``)."""

    model_config = ConfigDict(extra="forbid")

    dirs: list[str] = Field(default_factory=list)


class LabelTable(BaseModel):
    """``[labels.<key>]``: a label key the owner defines, for a product that has none of its own."""

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    values: list[str] = Field(default_factory=list)  # empty: any text
    reserved: bool = False  # locked: plugins may write it but not define it again
    writers: list[str] = Field(
        default_factory=list
    )  # who may set it (``user``, ``system:mailrules``); empty: anyone


class ApiSettings(BaseModel):
    """The authenticated door (API, MCP, gateways). The console is separate and loopback-only."""

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=0, ge=0, le=65535)  # 0: pick a free one and publish it
    remote_ok: bool = (
        False  # serve beyond loopback (MJ on the laptop): the token is then the gate
    )
    allowed_hosts: list[str] = Field(default_factory=list)
    dev_origin: str = ""
    rate_limit_per_minute: int = Field(default=600, ge=1)
    max_body_bytes: int = Field(default=1_000_000, ge=1024)
    event_ring_size: int = Field(default=1000, ge=10)
    heartbeat_seconds: float = Field(default=15.0, gt=0)
    mcp_enabled: bool = (
        True  # the master switch for the person's MCP door (agents have their own)
    )
    mcp_max_body_bytes: int = Field(default=256_000, ge=1024)
    mcp_max_concurrent: int = Field(default=8, ge=1)
    mcp_max_result_bytes: int = Field(default=256_000, ge=1024)


class ConsoleSettings(BaseModel):
    """The console always binds 127.0.0.1. There is no host setting on purpose: ``extra="forbid"``
    refuses one, so nothing in the config file can widen the bind."""

    model_config = ConfigDict(extra="forbid")

    port: int = Field(
        default=62536, ge=0, le=65535
    )  # OAKEN on a phone keypad; 0 picks a free one
    palette: Literal[
        "auto",
        "oak",
        "maple",
        "birch",
        "acorn",
        "moss",
        "stone",
        "sky",
        "stream",
        "contrast",
    ] = "auto"
    mode: Literal["auto", "light", "dark"] = "auto"


class RouterSettings(BaseModel):
    """The model router: the cost ladder's knobs. Backends are ``[plugins.llm_backend.<name>]``
    tables (``plugin``, ``url``, ``locality``, ``concurrency``); an analyzer's own ``route`` table
    names the backend and model it asks."""

    model_config = ConfigDict(extra="forbid")

    # LAN backends a confidential call may reach, by instance name. Local ones always may; hosted
    # ones never.
    lan_allowlist: list[str] = Field(default_factory=list)
    embedder: str = ""  # instance name; empty: the first healthy one
    vector_store: str = ""
    cache_store: str = (
        ""  # a cache_store plugin instance to use for answers; empty: core's own table
    )
    shortcuts: bool = (
        True  # near-duplicate reuse (only for analyzers that declare needs_vec)
    )
    near_duplicate_score: float = Field(default=0.97, ge=0, le=1)
    junk_index: bool = (
        False  # keep vector + hash of rejected items, to reject near matches later
    )
    nearjunk_score: float = Field(default=0.95, ge=0, le=1)
    min_confidence: float = Field(default=0.6, ge=0, le=1)  # below it, escalate once
    default_input_tokens: int = Field(
        default=2000, ge=1
    )  # an analyzer's input budget, unless it sets one
    default_output_tokens: int = Field(
        default=256, ge=1
    )  # a length cap, unless the request sets one
    call_timeout_seconds: float = Field(default=120.0, gt=0)
    wait_seconds: float = Field(
        default=30.0, gt=0
    )  # how long a job waits for an embedder or vector store


class Settings(BaseModel):
    """Core's whole configuration. Products add their sections through plugin ``config_model``s."""

    model_config = ConfigDict(extra="forbid")

    version: str = "0.0.0"
    log: LogSettings = Field(default_factory=LogSettings)
    jobs: JobsSettings = Field(default_factory=JobsSettings)
    swarm: SwarmSettings = Field(default_factory=SwarmSettings)
    router: RouterSettings = Field(default_factory=RouterSettings)
    console: ConsoleSettings = Field(default_factory=ConsoleSettings)
    operations: OperationsSettings = Field(default_factory=OperationsSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    queues: dict[str, QueueTable] = Field(
        default_factory=dict
    )  # in flow order: the console's nav follows it
    labels: dict[str, LabelTable] = Field(default_factory=dict)
    extensions: ExtensionsSettings = Field(default_factory=ExtensionsSettings)
    plugins: PluginTables = Field(default_factory=PluginTables)

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        """Read ``config.toml`` (a missing file is the defaults). Beside the core sections:
        ``[plugins.<kind>.<name>]`` are plugin tables, ``[active]`` picks the plugin for a kind,
        ``[mcp.allowed]`` is the operations turned on for MCP."""
        path = path or paths.config_dir() / "config.toml"
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        except OSError as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        return cls.from_text(text, source=str(path))

    @classmethod
    def from_text(cls, text: str, source: str = "config.toml") -> "Settings":
        """Settings from the text of a ``config.toml``: what ``load`` reads, and what the console's
        whole-file editor checks before it saves."""
        try:
            raw: dict[str, Any] = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"cannot read {source}: {exc}") from exc
        active, mcp, tables = (
            raw.pop("active", {}),
            raw.pop("mcp", {}),
            raw.pop("plugins", {}),
        )
        if not isinstance(mcp, dict) or set(mcp) - {"allowed"}:
            raise ConfigError(
                f"{source}: [mcp] has only [mcp.allowed]; the MCP switches are [api] mcp_enabled and the like"
            )
        if "allowed" in mcp:
            raw["operations"] = {
                **raw.get("operations", {}),
                "mcp_allowed": mcp["allowed"],
            }
        raw["plugins"] = {"active": active, "tables": tables}
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"{source}: {exc}") from exc
