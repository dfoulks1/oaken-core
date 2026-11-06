# SPDX-License-Identifier: Apache-2.0
"""The front door as an operation: put an item into intake (triage, then the ingestor). Without this only a plugin
could submit (``ctx.submit``), so a person at the CLI, an API client and a skin all had no way in."""

from pydantic import BaseModel, Field

from oaken_core.operations.context import OpContext
from oaken_core.operations.registry import Change, Preview, operation
from oaken_core.operations.services import need


class IntakeSubmitIn(BaseModel):
    source: str = Field(min_length=1, max_length=120, description="Where it came from, for the record: a name, never a secret.")
    body: str = Field(min_length=1, description="The item: text. Triagers read it; the ingestor stores it.")
    labels: dict[str, str] = Field(default_factory=dict, description="Labels set before triage, for example confidential=true.")
    format: str | None = Field(default=None, description="What the body is (json, ini, ...): a formatter of that name splits it into parts before triage.")


class IntakeSubmitOut(BaseModel):
    subject_id: str  # provisional: the real one is given when an ingestor stores it
    note: str


async def _preview(ctx: OpContext, inp: IntakeSubmitIn) -> Preview:
    return Preview(summary=f"Submit an item from {inp.source!r} ({len(inp.body):,} characters) to intake for triage",
                   changes=[Change(target="intake", field="submit", after=inp.source)], total_changes=1)  # fmt: skip


@operation("intake_submit", "Put an item into intake: triage decides, then the ingestor stores it. Returns its provisional id.",
           permission="mutate", input=IntakeSubmitIn, output=IntakeSubmitOut, mcp="off", group="intake",
           preview=_preview)  # fmt: skip
async def intake_submit(ctx: OpContext, inp: IntakeSubmitIn) -> IntakeSubmitOut:
    subject = await need(ctx.svc.intake, "intake").submit(inp.source, inp.body, labels=inp.labels, format=inp.format, actor=ctx.caller.actor)
    return IntakeSubmitOut(subject_id=str(subject), note="queued in Intake (default): it is split into parts, then triaged")


OPERATIONS = (intake_submit,)
