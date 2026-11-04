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
