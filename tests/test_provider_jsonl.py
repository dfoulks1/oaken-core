# SPDX-License-Identifier: Apache-2.0
import pytest
from oaken_core.clock import FakeClock
from oaken_core.errors import InvalidInput, PluginError
from oaken_core.plugins.contracts import PluginContext
from oaken_core.providers.jsonl import IngestIn, JsonlProvider

LINES = '{"company": "Acme", "signal": "hiring"}\n\n{"company": "Zed"}\nnot json\n[1, 2]\n'


class Intake:
    def __init__(self) -> None:
        self.items: list[tuple[str, object]] = []
        self.formats: list[str | None] = []
        self.fail_at: int | None = None

    async def submit(self, source: str, body: object, *, labels: dict[str, str], key: str | None = None, format: str | None = None) -> str:
        if self.fail_at is not None and len(self.items) == self.fail_at:
            raise RuntimeError("intake is not running")
        self.items.append((source, body))
        self.formats.append(format)
        return f"subject-{len(self.items)}"


async def plugin(intake: Intake, **config: object) -> JsonlProvider:
    p = JsonlProvider()
    await p.start(PluginContext(name="voltpop", kind="provider", clock=FakeClock(), log=None, config=dict(config), submit=intake.submit))  # type: ignore[arg-type]
    return p


async def test_every_good_line_becomes_an_item_with_the_parsed_json_as_its_body() -> None:
    intake = Intake()
    p = await plugin(intake)
    out = await p.ingest(None, IngestIn(text=LINES))  # type: ignore[arg-type]
    assert out.submitted == 3 and out.rejected == 1 and out.problems[0].line == 4  # a blank line is nothing; "not json" is line 4
    assert intake.items == [("voltpop", {"company": "Acme", "signal": "hiring"}), ("voltpop", {"company": "Zed"}), ("voltpop", [1, 2])]


async def test_raw_submits_the_text_and_source_comes_from_the_call_then_the_setting_then_the_name() -> None:
    intake = Intake()
    p = await plugin(intake, raw=True, source="from-config")
    await p.ingest(None, IngestIn(text="not json at all\n"))  # type: ignore[arg-type]
    await p.ingest(None, IngestIn(text="x\n", source="from-call"))  # type: ignore[arg-type]
    assert intake.items == [("from-config", "not json at all"), ("from-call", "x")]


async def test_the_preview_counts_without_submitting_and_too_many_lines_are_refused_whole() -> None:
    intake = Intake()
    p = await plugin(intake, max_lines=2)
    with pytest.raises(InvalidInput, match="max_lines"):
        await p._preview_ingest(None, IngestIn(text=LINES))  # type: ignore[arg-type]
    preview = await (await plugin(intake))._preview_ingest(None, IngestIn(text=LINES))  # type: ignore[arg-type]
    assert "3 item(s)" in preview.summary and "1 line(s)" in preview.summary and intake.items == []


async def test_a_failing_submit_says_where_it_stopped() -> None:
    intake = Intake()
    intake.fail_at = 1
    p = await plugin(intake)
    with pytest.raises(PluginError, match="line 3 after 1 submitted"):
        await p.ingest(None, IngestIn(text=LINES))  # type: ignore[arg-type]


def test_the_command_is_offered_as_a_mutating_operation_that_reads_its_text_from_a_pipe() -> None:
    (op,) = JsonlProvider().operations()
    assert op.name == "ingest" and op.permission == "mutate" and op.preview is not None
    assert IngestIn.model_json_schema()["properties"]["text"]["stdin"] is True


async def test_the_format_setting_goes_with_every_line_so_intake_knows_what_it_is() -> None:
    intake = Intake()
    p = await plugin(intake, format="json")
    await p.ingest(None, IngestIn(text='{"a": 1}\n{"b": 2}\n'))  # type: ignore[arg-type]
    assert intake.formats == ["json", "json"]
