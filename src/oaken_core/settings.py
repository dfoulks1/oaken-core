# SPDX-License-Identifier: Apache-2.0
"""Core's own settings. Written new: oaken-mail's ``settings.py`` (696 lines) is mail-shaped.

Only what core needs today. Products add their sections through the plugin ``config_model``.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Level = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class LogSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Level = "INFO"  # for everything under ``oaken_core``
    # Per-logger overrides by dotted name: {"oaken_core.jobs": "DEBUG"}. Python logging resolves
    # the most specific name, so one noisy module can be turned up without the rest.
    levels: dict[str, Level] = Field(default_factory=dict)
    format: Literal["console", "json"] = "console"
    file: str = ""  # also log to this file as JSON; relative paths sit under the data directory
    file_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    file_backups: int = Field(default=5, ge=0, le=100)

    @field_validator("level", mode="before")
    @classmethod
    def _upper(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v

    @field_validator("levels", mode="before")
    @classmethod
    def _upper_all(cls, v: object) -> object:
        return {k: s.upper() if isinstance(s, str) else s for k, s in v.items()} if isinstance(v, dict) else v


class JobsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_seconds: float = Field(default=60.0, gt=0)
    max_attempts: int = Field(default=3, ge=1)
    backoff_base_seconds: float = Field(default=5.0, ge=0)
    backoff_max_seconds: float = Field(default=900.0, ge=0)
    backoff_jitter: float = Field(default=0.2, ge=0, le=1)  # a fraction taken off, at random
    handler_timeout_seconds: float = Field(default=600.0, ge=0)  # 0 turns the timeout off
    max_result_bytes: int = Field(default=65536, ge=1)
    kind_limits: dict[str, int] = Field(default_factory=dict)  # concurrency cap per job kind
    requeue_cooldown_seconds: float = Field(default=60.0, ge=0)  # per job, between requeues
    # Intake: when a triager fails on the last attempt, treat the item as "unsure" and ingest it
    # (oaken-mail's behaviour). Off by default: a broken triager lets intake back up.
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
    default_max_per_hour: int = Field(default=600, ge=1)  # jobs one agent may claim per hour
    # How long an agent holds a job before it must heartbeat. Long: a large model on modest
    # hardware is slow.
    agent_lease_seconds: float = Field(default=600.0, gt=0)


class PluginTables(BaseModel):
    """The ``[plugins]`` part of the config: a table per plugin, ``[plugins.<kind>.<name>]``, and
    which plugin is active for a kind that takes exactly one (``active``).

    A table that carries ``plugin = "<entry point>"`` is a *named instance* of that plugin: any
    number of them, each with its own settings (``[plugins.llm_backend.desk]`` and
    ``[plugins.llm_backend.laptop]`` both with ``plugin = "ollama"``).
    """

    model_config = ConfigDict(extra="forbid")

    active: dict[str, str] = Field(default_factory=dict)  # kind -> plugin, e.g. {"store": "sqlite"}
    tables: dict[str, dict[str, dict[str, object]]] = Field(default_factory=dict)  # kind -> name -> table

    def table(self, kind: str, name: str) -> dict[str, object]:
        return dict(self.tables.get(kind, {}).get(name, {}))

    def instances(self, kind: str) -> dict[str, str]:
        """Instance name -> the plugin it is an instance of, for tables with a ``plugin`` key."""
        return {n: str(t["plugin"]) for n, t in self.tables.get(kind, {}).items() if "plugin" in t}


class OperationsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_ttl_seconds: float = Field(default=300.0, gt=0)  # how long a confirmation token lives
    max_pending_confirmations: int = Field(default=64, ge=1)
    proposal_changes_shown: int = Field(default=20, ge=1)  # changes listed in a proposal
    # ``[mcp.allowed]``: operations (or ``plugin:<name>``) the config turns on or off for MCP. The
    # ``never`` ceiling is not touched by these; only ``lift_never`` below lifts one.
    mcp_allowed: dict[str, bool] = Field(default_factory=dict)
    # Operations whose ``mcp="never"`` ceiling the config file lifts, by name, to ``off`` (then an
    # exposure setting or an agent grant can turn them on). Logged at start. Only a person who can
    # edit the file can do this; no operation can.
    lift_never: list[str] = Field(default_factory=list)


class ApiSettings(BaseModel):
    """The authenticated door (API, MCP, gateways). The console is separate and loopback-only."""

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=0, ge=0, le=65535)  # 0: pick a free one and publish it
    remote_ok: bool = False  # serve beyond loopback (MJ on the laptop): the token is then the gate
    allowed_hosts: list[str] = Field(default_factory=list)
    dev_origin: str = ""
    rate_limit_per_minute: int = Field(default=600, ge=1)
    max_body_bytes: int = Field(default=1_000_000, ge=1024)
    event_ring_size: int = Field(default=1000, ge=10)
    heartbeat_seconds: float = Field(default=15.0, gt=0)
    mcp_enabled: bool = True  # the master switch for the person's MCP door (agents have their own)
    mcp_max_body_bytes: int = Field(default=256_000, ge=1024)
    mcp_max_concurrent: int = Field(default=8, ge=1)
    mcp_max_result_bytes: int = Field(default=256_000, ge=1024)


class Settings(BaseModel):
    """Core's whole configuration. Products add their sections through plugin ``config_model``s."""

    model_config = ConfigDict(extra="forbid")

    version: str = "0.0.0"
    log: LogSettings = Field(default_factory=LogSettings)
    jobs: JobsSettings = Field(default_factory=JobsSettings)
    swarm: SwarmSettings = Field(default_factory=SwarmSettings)
    operations: OperationsSettings = Field(default_factory=OperationsSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    plugins: PluginTables = Field(default_factory=PluginTables)
