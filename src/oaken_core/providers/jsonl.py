# SPDX-License-Identifier: Apache-2.0
"""``jsonl``: JSON Lines in, items in intake out. The by-hand door: a person dumps a file in; nothing keeps it open
(``socket`` is the machine's door).

One command, ``oaken jsonl ingest``: pipe a file or another tool's output into it and every line becomes one
item for triage, with the parsed JSON object as its body (or the line's text, with ``raw``). It also answers on
the API as the operation ``jsonl.ingest``. The plugin has no file watcher and no loop: the thing that makes
the lines decides when to send them.

    cat leads.jsonl | oaken jsonl ingest --yes

shortcut: no de-duplication, so piping the same file twice submits every line twice; and one call is as big as
the API's body limit (``[api] max_body_bytes``, 1 MB by default), so split a bigger file (``split -l 5000``) or
raise the limit. Upgrade when a source re-sends lines.
"""

import json
from typing import Any, ClassVar

from oaken_core.errors import InvalidInput, PluginError
from oaken_core.plugins.base import BaseProvider, command
from oaken_core.plugins.contracts import PluginChange, PluginOpContext, PluginPreview
from pydantic import BaseModel, ConfigDict, Field

PROBLEMS_SHOWN = 20  # the first bad lines are named; the rest are only counted


class JsonlConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(default="", description="What the record calls where these came from. Empty: this plugin's name.")
    format: str = Field(default="", description="What the items are (json, ini, a format a formatter plugin declares): it splits each into parts before triage. Empty: a JSON object is split by key, anything else stays whole.")
    raw: bool = Field(default=False, description="Submit each line as the text it is, not as the parsed JSON. Off: the parsed value.")
    max_lines: int = Field(default=5000, ge=1, le=100000, description="The most lines one call will take. A bigger one is refused whole.")


class IngestIn(BaseModel):
    text: str = Field(json_schema_extra={"stdin": True}, description="JSON Lines: one JSON value on each line. On the command line, pipe it in.")
    source: str | None = Field(default=None, description="Override the source for this call.")


class Problem(BaseModel):
    line: int
    reason: str


class IngestOut(BaseModel):
    submitted: int
    rejected: int
    problems: list[Problem]  # the first few rejected lines, by number


def _parse(text: str, raw: bool) -> tuple[list[tuple[int, Any]], list[Problem], int]:
    """(line number, body) for each good line, the problems, and how many problems there were in all."""
    good: list[tuple[int, Any]] = []
    problems: list[Problem] = []
    bad = 0
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if raw:
            good.append((number, line))
            continue
        try:
            good.append((number, json.loads(line)))
        except ValueError as exc:
            bad += 1
            if len(problems) < PROBLEMS_SHOWN:
                problems.append(Problem(line=number, reason=f"not JSON: {exc}"))
    return good, problems, bad


class JsonlProvider(BaseProvider):
    version: ClassVar[str] = "0.1.0"
    config_model = JsonlConfig
    config: JsonlConfig

    def _source(self, inp: IngestIn) -> str:
        return inp.source or self.config.source or self.ctx.name

    def _good(self, inp: IngestIn) -> tuple[list[tuple[int, Any]], list[Problem], int]:
        good, problems, bad = _parse(inp.text, self.config.raw)
        if len(good) > self.config.max_lines:
            raise InvalidInput(f"{len(good)} lines is more than max_lines ({self.config.max_lines}): split it, or raise the setting")
        return good, problems, bad

    async def _preview_ingest(self, ctx: PluginOpContext, inp: IngestIn) -> PluginPreview:
        good, _problems, bad = self._good(inp)
        skip = f"; {bad} line(s) that are not JSON are skipped" if bad else ""
        return PluginPreview(f"Submit {len(good)} item(s) from {self._source(inp)!r} to intake{skip}",
                             (PluginChange(self._source(inp), "submit", after=len(good)),), len(good))  # fmt: skip

    @command("ingest", "Put each line of JSON Lines into intake as an item. Pipe it in: cat leads.jsonl | oaken jsonl ingest --yes",
             input=IngestIn, output=IngestOut, permission="mutate", preview="_preview_ingest")  # fmt: skip
    async def ingest(self, ctx: PluginOpContext, inp: IngestIn) -> IngestOut:
        good, problems, bad = self._good(inp)
        source = self._source(inp)
        done = 0
        for number, body in good:
            try:
                await self.submit(body, source=source, format=self.config.format or None)
            except Exception as exc:  # noqa: BLE001 - say where it stopped; a rerun would repeat the ones before it
                raise PluginError(f"stopped at line {number} after {done} submitted: {type(exc).__name__}: {exc}") from None
            done += 1
        return IngestOut(submitted=done, rejected=bad, problems=problems)
