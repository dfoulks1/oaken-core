# SPDX-License-Identifier: Apache-2.0
"""The request and response bodies that are not operations. The routes validate with these and the
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
    marks_read: bool
    streaming: bool
    mcp: Literal["never", "off", "on"]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class OperationList(BaseModel):
    operations: list[OperationInfo]


class ClaimRequest(_Body):
    kinds: list[str] | None = Field(
        default=None, max_length=64, description="Job kinds to claim; default: all of the agent's."
    )
    max_chars: int = Field(
        default=12000,
        ge=500,
        le=200000,
        description="For an ingest.enrich job: the most message text to send.",
    )


class JobLease(BaseModel):
    id: str
    kind: str
    account_id: str | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    lease_expires_at: str | None


class ClaimResponse(BaseModel):
    job: JobLease | None


class HeartbeatResponse(BaseModel):
    ok: bool = True
    cancel_requested: bool


class CompleteRequest(_Body):
    result: dict[str, Any] | None = None
    produced_by: str | None = Field(default=None, max_length=128)


class FailRequest(_Body):
    error: str = Field(max_length=2000)
    permanent: bool = False
    retry: bool = Field(default=False, description="Hand the job back without spending an attempt.")


class FailResponse(BaseModel):
    status: Literal["queued", "failed"]


class CompleteResponse(BaseModel):
    ok: bool = True
    stored: int = 0
    still_owed: int = 0


class Ok(BaseModel):
    ok: bool = True
