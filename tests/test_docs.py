# SPDX-License-Identifier: Apache-2.0
"""`plugin describe` and the Docs pages: documentation generated from the plugins in the stack and from core, nothing written by hand."""

from pathlib import Path

import httpx2
import pytest
from markupsafe import escape
from pydantic import BaseModel, Field

from oaken_core.errors import NotFound
from oaken_core.operations.caller import CallerContext
from oaken_core.plugins.base import BaseTemplatePlugin

from .test_console import BASE, EP, make_core, stub

CLI = CallerContext.cli()


class WatchCfg(BaseModel):
    topic: str = Field(description="What this instance watches for.")
    style: str = Field(default="plain", description="How it speaks.")


class Watcher(BaseTemplatePlugin, stub("enricher")):  # type: ignore[misc]
    """Watches for one topic. An instance is made for each topic."""

    contextual = ("topic",)
    config_model = WatchCfg
    setup_help = "Give each instance its own topic."
    config_example = '[plugins.enricher.jobs]\nplugin = "watcher"\ntopic = "hiring"'


async def make(tmp_path: Path):  # type: ignore[no-untyped-def]
    return await make_core(tmp_path, {}, {"enrich": {"kind": "enricher"}}, extra=[EP("watcher", Watcher)])


async def test_plugin_describe_is_what_the_plugin_says_of_itself(tmp_path: Path) -> None:
    core = await make(tmp_path)
    try:
        out = (await core.client.call("plugin_describe", {"name": "watcher"}, CLI)).output
        (p,) = out.plugins
        assert p.summary == "Watches for one topic. An instance is made for each topic." and p.setup_help == "Give each instance its own topic."
        assert p.config_example.startswith("[plugins.enricher.jobs]") and p.contextual == ["topic"] and p.template
        assert {(s.name, s.required, s.description) for s in p.settings} == {("topic", True, "What this instance watches for."), ("style", False, "How it speaks.")}  # from its config schema
        assert "a template" in out.text and "Give each instance its own topic." in out.text and "What this instance watches for." in out.text
        assert p.template_note == Watcher.template_help and "does not run as itself" in p.template_note  # core's own account of what a template is, to the person running it
        notes = (await core.client.call("plugin_describe", {"name": "notes"}, CLI)).output.plugins[0]
        assert notes.template_note == "" and not notes.template  # an ordinary plugin has none
    finally:
        await core.host.stop()


async def test_with_no_name_it_is_an_overview_of_this_stack_and_an_unknown_name_says_so(tmp_path: Path) -> None:
    core = await make(tmp_path)
    try:
        out = (await core.client.call("plugin_describe", {}, CLI)).output
        assert {"watcher", "notes", "sqlite"} <= {p.name for p in out.plugins} and "plugins in this stack" in out.text
        with pytest.raises(NotFound, match="no plugin named 'ghost' in this stack"):
            await core.client.call("plugin_describe", {"name": "ghost"}, CLI)
    finally:
        await core.host.stop()


async def test_the_docs_pages_are_built_from_the_plugins_and_from_core(tmp_path: Path) -> None:
    core = await make(tmp_path)
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=core.console), base_url=BASE)
    try:
        index = (await client.get("/docs")).text
        assert 'href="/docs"' in (await client.get("/")).text  # a Docs link in the navbar
        for q in core.client.svc.queues.all():
            assert str(escape(q.description)) in index  # each stage's description is core's own, hidden stages included
        for k in (await core.client.call("label_keys", {}, CLI)).output.keys:
            assert str(escape(k.description)) in index  # and each label's: core's, a plugin's or the owner's
        assert "Watches for one topic." in index and 'href="/plugins/enricher/watcher"' in index  # each plugin's own first line, linking to the plugin's own page
        assert index.index(">Intake<") < index.index(">store<") < index.index(">embed<")  # stages in the order an item moves: Intake, then store and embed last
        page = (await client.get("/plugins/enricher/watcher")).text  # the plugin's documentation is on the plugin's own page
        assert "Give each instance its own topic." in page and "it does not run as itself" in page and 'plugin = &#34;watcher&#34;' in page  # setup, core's template note, the example
        everything = (await client.get("/docs/all")).text
        assert "Watches for one topic." in everything and 'id="store-sqlite"' in everything  # every plugin on one page
        assert (await client.get("/docs/enricher/watcher")).status_code == 404  # no second copy of it under Docs
    finally:
        await core.host.stop()


def test_the_command_line_prints_documentation_as_text(capsys: pytest.CaptureFixture[str]) -> None:
    from oaken_core.cli.commands import _human

    _human({"text": "llm (triager) v1\n\nWhat it is.\n", "plugins": []})
    assert capsys.readouterr().out.startswith("llm (triager) v1")  # not JSON


async def test_no_page_uses_an_inline_style_because_the_console_forbids_them(tmp_path: Path) -> None:
    core = await make(tmp_path)
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=core.console), base_url=BASE)
    try:
        for path in ("/", "/docs", "/docs/all", "/plugins/enricher/watcher", "/stage/enrich", "/log", "/pipeline", "/capabilities", "/config"):
            page = await client.get(path)
            assert ' style="' not in page.text, path  # style-src 'self': an inline style is blocked, and the page looks wrong
        css = (await client.get("/static/oaken.css")).text
        assert ".doc-text p {" in css and ".file-path {" in css
    finally:
        await core.host.stop()


def test_a_plugins_text_becomes_paragraphs_code_blocks_and_code_spans_and_is_never_markup() -> None:
    from oaken_core.console.docs_text import docs_html

    html = str(docs_html("`x` does a thing\nacross two lines.\n\n    [plugins.a]\n    b = 1\n\nUse `oaken run` <b>now</b>."))
    assert "<p><code>x</code> does a thing across two lines.</p>" in html  # a hard-wrapped paragraph is one paragraph
    assert '<pre class="mono">[plugins.a]\nb = 1</pre>' in html  # an indented block is kept as code
    assert "<code>oaken run</code>" in html and "&lt;b&gt;now&lt;/b&gt;" in html  # a span is code; anything else a plugin wrote is escaped


async def test_the_providers_tab_offers_the_plus_for_a_provider_that_is_a_template(tmp_path: Path) -> None:
    from oaken_core.plugins.base import BaseProvider

    class Account(BaseTemplatePlugin, BaseProvider):
        """A source per account."""

        contextual = ("account",)
        config_model = WatchCfg

    class Plain(BaseProvider):
        """A source that is just itself."""

    core = await make_core(tmp_path, {}, {"enrich": {"kind": "enricher"}}, groups={"oaken_core.providers": [EP("acct", Account), EP("plain", Plain)]})
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=core.console), base_url=BASE)
    try:
        page = (await client.get("/pipeline/providers")).text
        assert 'popovertarget="new-instance"' in page and 'action="/ui/plugins/provider/acct/instance"' in page  # a provider has no stage: the + is on the Providers tab
        assert 'name="stage"' not in page  # and its dialog asks for no stage
        assert 'action="/ui/plugins/provider/plain/instance"' not in page and page.count('/instance"') == 1  # an ordinary provider (no template mixin) gets none: only the template does
        assert 'popovertarget="new-instance"' not in (await client.get("/pipeline/analyzers")).text
    finally:
        await core.host.stop()
