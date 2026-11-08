# SPDX-License-Identifier: Apache-2.0
"""The request and response bodies that are not operations. (The agent job routes use the job
operations' own models, so there is one definition.) The routes validate with these and the
OpenAPI document is generated from them, so the two cannot differ."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Health(BaseModel):
    status: Literal["ok"] = "ok"


class CallRequest(_Body):
    input: dict[str, Any] = Field(default_factory=dict)
    confirm: bool = False
    confirmation_token: str | None = Field(default=None, max_length=512)


class OperationInfo(BaseModel):
    name: str
    summary: str
    permission: Literal["read", "mutate", "send"]
    group: str = ""
    plugin: str | None = None
    template: str | None = None
    agent_ok: bool = False
    streaming: bool
    mcp: Literal["never", "off", "on"]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class OperationList(BaseModel):
    operations: list[OperationInfo]


class Ok(BaseModel):
    ok: bool = True
