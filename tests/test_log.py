# SPDX-License-Identifier: Apache-2.0
"""The kept log: every line goes on the bus, a table keeps it, and `log_tail` reads it in widening scope."""

import asyncio
import json
import logging
from pathlib import Path

import httpx2

from oaken_core.events import LogEvent
from oaken_core.logging import get_logger, log_context
from oaken_core.logstore import plugin_of, read_file
from oaken_core.operations.caller import CallerContext

from .test_console import BASE, make_core

CLI = CallerContext.cli()


async def kept(core, **scope):  # type: ignore[no-untyped-def]
    await asyncio.sleep(0.05)  # the bus hands the lines to the writer
    await core.logtable.flush()
    return (await core.client.call("log_tail", scope, CLI)).output


async def test_lines_are_kept_and_read_back_in_widening_scope(tmp_path: Path) -> None:
    core = await make_core(tmp_path, {}, {"enrich": {"kind": "enricher"}})
    try:
        logging.getLogger("oaken_core.plugin.enricher.notes").warning("notes said hi")
        logging.getLogger("oaken_core.plugin.enricher.broken").warning("broken said hi")
        with log_context(queue="enrich"):
            get_logger("oaken_core.jobs").warning("a job ran in enrich")
        get_logger("oaken_core.jobs").warning("a job ran somewhere else", queue="default")
        mine = await kept(core, plugin="enricher/notes")
        assert [ln.event for ln in mine.lines] == ["notes said hi"] and mine.kept_in == "table"  # plugin: its own lines
        stage = await kept(core, stage="enrich")
        assert {ln.event for ln in stage.lines} == {"a job ran in enrich", "notes said hi", "broken said hi"}  # stage: its jobs, and the plugins running in it
        everything = await kept(core)
        assert {"notes said hi", "broken said hi", "a job ran in enrich", "a job ran somewhere else"} <= {ln.event for ln in everything.lines}  # all of it
        assert [ln.event for ln in everything.lines] == sorted([ln.event for ln in everything.lines], key=[ln.event for ln in everything.lines].index)  # oldest first
        errors = await kept(core, level="error")
        assert not [ln for ln in errors.lines if ln.event == "notes said hi"]  # a warning is below error
    finally:
        await core.host.stop()


async def test_log_lines_are_on_the_bus_for_a_stream_that_asks_and_never_in_the_default_stream(tmp_path: Path) -> None:
    core = await make_core(tmp_path, {}, {})
    try:
        asked, default = core.bus.stream(LogEvent), core.bus.stream()
        get_logger("oaken_core.jobs").warning("seen live")
        await asyncio.sleep(0.01)
        assert any(isinstance(e, LogEvent) and e.event == "seen live" for e in asked._items)  # `oaken log tail -f` sees it
        assert not any(isinstance(e, LogEvent) for e in default._items)  # a plain watcher is not flooded
        await asked.aclose()
        await default.aclose()
    finally:
        await core.host.stop()


async def test_the_log_has_a_page_of_its_own_in_plain_text_a_button_to_it_on_each_page_and_a_json_export(tmp_path: Path) -> None:
    core = await make_core(tmp_path, {}, {"enrich": {"kind": "enricher"}})
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=core.console), base_url=BASE)
    try:
        logging.getLogger("oaken_core.plugin.enricher.notes").warning("notes said hi")
        get_logger("oaken_core.jobs").warning("elsewhere")
        await kept(core)
        for path, href in (("/plugins/enricher/notes", "/log?plugin=enricher/notes"), ("/stage/enrich", "/log?stage=enrich"), ("/", "/log")):
            page = (await client.get(path)).text
            klass = "btn btn-secondary btn-sm" if path.startswith("/stage") else "btn btn-secondary btn-sm log-btn"  # on a stage it sits among the stage's buttons; elsewhere at the right of the title
            assert f'class="{klass}" href="{href}"' in page  # a Secondary button on each page, to the log of that scope
            assert 'id="log" popover' not in page  # and no log on the page itself
        log = (await client.get("/log", params={"plugin": "enricher/notes"})).text
        assert '<pre class="log">' in log and "enricher/notes  notes said hi" in log and "elsewhere" not in log  # plain text, its own scope
        assert 'href="/log/export?plugin=enricher%2Fnotes"' in log and "Export as JSON" in log and "Everything" in log and "Its stage" in log  # export, and widening
        assert "Kept in core's database" in log  # no file: it says where
        export = await client.get("/log/export", params={"plugin": "enricher/notes"})
        assert export.headers["content-disposition"] == 'attachment; filename="oaken-log.json"'
        assert [ln["event"] for ln in export.json()["lines"]] == ["notes said hi"] and export.json()["kept_in"] == "table"
        assert "elsewhere" in (await client.get("/log")).text  # everything
    finally:
        await core.host.stop()


async def test_a_log_file_is_shown_prominently_when_it_exists(tmp_path: Path) -> None:
    from oaken_core.operations.builtin.logs import log_tail  # noqa: F401 - the file case is the operation's; the page shows what it says

    path = tmp_path / "oaken.log"
    path.write_text(json.dumps({"ts": "2026-01-01T00:00:00.000+00:00", "level": "info", "logger": "oaken_core.plugin.triager.llm", "event": "hi"}) + "\n")
    core = await make_core(tmp_path / "c", {}, {})
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=core.console), base_url=BASE)
    try:
        core.client.svc.settings = core.client.svc.settings.model_copy(update={"log": core.client.svc.settings.log.model_copy(update={"file": str(path)})})
        page = (await client.get("/log", params={"plugin": "triager/llm"})).text
        assert str(path) in page and "Log file" in page and "triager/llm  hi" in page  # the path first, then the text
    finally:
        await core.host.stop()


def test_a_plugins_logger_names_it() -> None:
    assert plugin_of("oaken_core.plugin.triager.llm") == "triager/llm" and plugin_of("oaken_core.jobs") == ""


def test_the_file_log_is_read_back_newest_last_and_a_bad_line_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "oaken.log"
    path.write_text("\n".join([json.dumps({"ts": "2026-01-01T00:00:00.000+00:00", "level": "info", "logger": "oaken_core.x", "event": n}) for n in ("a", "b")] + ["not json"]) + "\n")
    assert [r["event"] for r in read_file(path, 10)] == ["a", "b"]


async def test_logging_can_be_turned_off_and_log_tail_says_so(tmp_path: Path) -> None:
    from oaken_core.settings import Settings

    core = await make_core(tmp_path, {}, {})
    try:
        core.client.svc.settings = core.client.svc.settings.model_copy(update={"log": core.client.svc.settings.log.model_copy(update={"enabled": False})})
        out = (await core.client.call("log_tail", {}, CLI)).output
        assert out.kept_in == "off" and out.lines == []
        assert Settings().log.enabled is True  # on unless asked
    finally:
        await core.host.stop()


async def test_a_long_log_does_not_touch_a_short_page_and_the_theme_has_its_three_kinds_of_button(tmp_path: Path) -> None:
    core = await make_core(tmp_path, {}, {})
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=core.console), base_url=BASE)
    try:
        for i in range(60):
            get_logger("oaken_core.jobs").warning(f"line {i}")
        await kept(core)
        page = (await client.get("/")).text
        assert "line 59" not in page and '<details class="card fold" id="fold-g-" open>' in page  # the log is not on the page: a long one changes nothing
        assert (await client.get("/log")).text.count("line ") >= 60  # it is on its own
        css = (await client.get("/static/oaken.css")).text
        assert "--secondary:" in css and "--row:" in css and ".btn-secondary" in css and "var(--accent) 9%, transparent" in css  # primary, secondary, row
        assert ".log-btn { float: right" in css and css.count(":not(.btn-secondary)") == 2 and "font-family: var(--font)" in css  # the tint every other button gets does not reach a Secondary one, and a button in a heading is not set in its serif
        assert "--secondary-border: var(--border)" in css and ".btn-secondary { box-shadow: var(--shadow-1); }" in css  # a Secondary button looks like a card: its surface, border and shadow
        assert ".btn-bare { background: transparent" in css and ":not(.btn-ghost)" not in css  # ghost is the accent tint; the chrome-less close X is btn-bare
    finally:
        await core.host.stop()


async def test_the_config_editors_buttons_are_primary_ghost_and_secondary_as_asked(tmp_path: Path) -> None:
    core = await make_core(tmp_path, {}, {})
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=core.console), base_url=BASE)
    try:
        page = (await client.get("/config")).text
        assert '<button class="btn btn-primary" formaction="/ui/config-file/test">Test</button>' in page
        assert '<button class="btn btn-ghost">Save</button>' in page
        assert 'class="btn btn-secondary" formaction="/ui/config-file/reload"' in page
        assert '<a class="btn btn-primary right" href="/config">Revert to saved</a>' in page
    finally:
        await core.host.stop()


def dialogs(page: str) -> list[str]:
    """Each ``<div class="dialog" ...>`` with everything inside it, found by matching its own closing tag."""
    import re

    found = []
    for m in re.finditer(r'<div class="dialog[ "]', page):
        depth, pos = 0, m.start()
        for tag in re.finditer(r"<(/?)div\b", page[m.start():]):
            depth += -1 if tag.group(1) else 1
            if depth == 0:
                pos = m.start() + tag.end()
                break
        found.append(page[m.start(): pos])
    return found


async def test_buttons_in_a_modal_are_ghost_the_buttons_that_control_a_stage_are_primary_and_log_sits_between_them(tmp_path: Path) -> None:
    import re

    core = await make_core(tmp_path, {}, {"enrich": {"kind": "enricher"}})
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=core.console), base_url=BASE)
    try:
        stage = (await client.get("/stage/enrich")).text
        stop = re.search(r'<button class="([^"]*)" title="(?:Let it hand out|Stop handing out)', stage)
        assert stop and "btn-primary" in stop.group(1)  # Stop / Start controls the stage
        row = stage[stage.index("Stop handing out") if "Stop handing out" in stage else stage.index("Let it hand out"):]
        assert row.index('href="/log?stage=enrich"') < row.index('popovertarget="queue-settings"')  # Start/Stop, then Log, then Settings
        assert 'btn-secondary btn-sm log-btn' not in stage  # and it is not floated into the title row on a stage
        for path in ("/stage/enrich", "/", "/pipeline/labels", "/plugins/enricher/notes"):
            for dialog in dialogs((await client.get(path)).text):
                primaries = re.findall(r'<[a-z]+ class="[^"]*btn-primary[^"]*"[^>]*>', dialog)
                assert all('/try"' in p for p in primaries), (path, primaries)  # a modal's buttons are Ghost; the one Primary is a settings dialog's Test
                if "/try" in dialog:  # [Test: Primary] [X: Secondary] [check: Ghost]: do this, then one of those
                    foot = dialog[dialog.index('<div class="dialog-foot">'):]
                    assert foot.index("btn btn-primary") < foot.index("btn btn-secondary btn-icon") < foot.index('btn btn-ghost btn-icon btn-equal" type="submit"')
                    assert foot.count("btn-equal") == 3  # and the three are one size
        assert 'class="btn btn-secondary btn-sm log-btn" href="/log"' in (await client.get("/")).text  # General keeps it at the right of the title
    finally:
        await core.host.stop()
