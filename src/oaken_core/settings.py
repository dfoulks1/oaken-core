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
