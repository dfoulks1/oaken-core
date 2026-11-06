# SPDX-License-Identifier: Apache-2.0
"""``oaken-plugins``: one distribution, a subpackage per plugin, nothing imported until it is added."""

import sys
from typing import Any
from pathlib import Path

import pytest

from oaken_core.cli.commands import main
from oaken_core.plugins.host import PluginTables
from oaken_core.plugins import catalog
from oaken_core.plugins.kinds import KindRegistry

from .test_console import Rig, make_core, rig  # noqa: F401 - a started core
from .test_plugin_add import CLI

KINDS = [k.name for k in KindRegistry.shipped()]


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An ``oaken_plugins`` with one formatter and one triager. Importing either one would leave a mark."""
    root = tmp_path / "oaken_plugins"
    (root / "formatter_demo").mkdir(parents=True)
    (root / "triager_big_one").mkdir()
    (root / "__init__.py").write_text("")
    (root / "formatter_demo" / "__init__.py").write_text(
        '"""Splits demo text into lines.\n\nMore."""\nimport sys\nsys.modules["demo_imported"] = True\n'
        "from oaken_core.plugins.base import BaseFormatter\n\n\nclass DemoFormatter(BaseFormatter):\n    version = '1'\n    formats = ('demo',)\n\n    async def split(self, raw):\n        return {}\n")
    (root / "triager_big_one" / "__init__.py").write_text('"""A big one."""\nraise RuntimeError("never imported to list")\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    for m in [m for m in sys.modules if m.startswith("oaken_plugins")]:
        monkeypatch.delitem(sys.modules, m)
    sys.modules.pop("demo_imported", None)
    return root


def test_listing_names_each_plugin_and_imports_none_of_them(fake: Path) -> None:
    found = catalog.entries(KINDS)
    assert [(e.kind, e.name, e.summary) for e in found] == [("formatter", "demo", "Splits demo text into lines."), ("triager", "big-one", "A big one.")]
    assert "demo_imported" not in sys.modules  # read, never run


def test_oaken_plugins_prints_the_list_and_says_when_it_is_missing(fake: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    assert main(["plugins"]) == 0
    out = capsys.readouterr().out
    assert "demo" in out and "Splits demo text" in out and "plugin add KIND.NAME" in out
    monkeypatch.setattr(catalog, "installed", lambda: False)
    monkeypatch.setattr(catalog, "entries", lambda kinds: [])
    main(["plugins"])
    assert "pip install oaken-plugins" in capsys.readouterr().out


async def test_plugin_add_finds_a_name_in_oaken_plugins_and_imports_only_that_one(rig: Rig, fake: Path, oaken_home: Path) -> None:  # noqa: F811
    done = await rig.core.client.call("plugin_add", {"plugin": "demo"}, CLI, confirm=True)
    assert done.output.kind == "formatter" and done.output.name == "demo"  # type: ignore[union-attr]
    assert "formatter_demo" in (oaken_home / "config" / "config.toml").read_text() or "oaken_plugins" in (oaken_home / "config" / "config.toml").read_text()
    assert "oaken_plugins.triager_big_one" not in sys.modules  # the other plugin was never touched


async def test_a_name_oaken_plugins_does_not_have_lists_what_it_does(rig: Rig, fake: Path) -> None:  # noqa: F811
    with pytest.raises(Exception, match=r"no plugin named 'ghost'.*in oaken-plugins: demo, big-one"):
        await rig.core.client.call("plugin_add", {"plugin": "ghost"}, CLI, confirm=True)


async def test_a_directory_is_looked_in_first_and_oaken_plugins_when_none_is_set_or_it_has_no_such_plugin(rig: Rig, fake: Path, tmp_path: Path) -> None:  # noqa: F811
    host = rig.core.host
    plugins = tmp_path / "plugins"
    (plugins / "demo" / "src").mkdir(parents=True)
    (plugins / "demo" / "pyproject.toml").write_text('[project.entry-points."oaken_core.formatters"]\ndemo = "somewhere:Demo"\n')
    host.extensions_dirs = [str(plugins)]
    assert "class" not in host.plan_add("demo").table  # the directory's plugin: found by its entry point, not as a class path
    host.extensions_dirs = []
    assert host.plan_add("demo").table["class"] == "oaken_plugins.formatter_demo:DemoFormatter"  # no directory: oaken-plugins
    host.extensions_dirs = [str(tmp_path / "missing")]  # set, but not there: skipped, then oaken-plugins
    assert host.plan_add("demo").table["class"].startswith("oaken_plugins.")


async def test_the_not_found_message_tells_a_plugin_author_a_local_folder_is_enough(rig: Rig, fake: Path) -> None:  # noqa: F811
    rig.core.host.extensions_dirs = []
    with pytest.raises(Exception, match=r"\[extensions\] dirs.*never has to be on PyPI"):
        rig.core.host.plan_add("ghost")


def test_oaken_plugins_lists_the_extensions_directories_first_then_the_package(fake: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, oaken_home: Path) -> None:
    mine = tmp_path / "mine" / "oaken-formatter-shiny"
    (mine / "src").mkdir(parents=True)
    (mine / "pyproject.toml").write_text('[project]\nname = "x"\ndescription = "My shiny formatter."\n[project.entry-points."oaken_core.formatters"]\nshiny = "somewhere:Shiny"\n')
    (oaken_home / "config").mkdir(parents=True, exist_ok=True)
    (oaken_home / "config" / "config.toml").write_text(f'[extensions]\ndirs = ["{tmp_path / "mine"}"]\n')
    assert main(["plugins"]) == 0
    out = capsys.readouterr().out
    assert "shiny" in out and "My shiny formatter." in out and "demo" in out  # a plugin in a directory, and the package's
    assert out.index("shiny") < out.index("demo")  # the directory first


def test_naming_a_checkout_of_oaken_plugins_in_extensions_dirs_is_enough_to_list_and_add_from_it(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, oaken_home: Path) -> None:
    repo = tmp_path / "checkout"
    pkg = repo / "src" / "oaken_plugins" / "formatter_checked"
    pkg.mkdir(parents=True)
    (repo / "src" / "oaken_plugins" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text('"""From a checkout."""\n')
    (oaken_home / "config").mkdir(parents=True, exist_ok=True)
    (oaken_home / "config" / "config.toml").write_text(f'[extensions]\ndirs = ["{repo}"]\n')
    for m in [m for m in sys.modules if m.startswith("oaken_plugins")]:
        monkeypatch.delitem(sys.modules, m)
    monkeypatch.setattr(sys, "path", list(sys.path))  # prepare() appends; leave it as it was
    assert main(["plugins"]) == 0
    out = capsys.readouterr().out
    assert "checked" in out and "From a checkout." in out


async def _host(tables: dict[str, Any]):  # type: ignore[no-untyped-def]
    from oaken_core.clock import FakeClock
    from oaken_core.events import EventBus
    from oaken_core.plugins.host import PluginHost

    host = PluginHost(PluginTables(tables=tables), FakeClock(), EventBus(), discover=lambda g: [])
    await host.load()
    return host


async def test_settings_for_a_plugin_oaken_plugins_offers_load_it_with_no_class_line(fake: Path) -> None:
    host = await _host({"formatter": {"demo": {"x": 1}}})
    rec = host._records[("formatter", "demo")]
    assert not rec.missing and rec.instance is not None  # found in the catalog by kind and name


async def test_a_settings_table_with_no_plugin_behind_it_is_listed_inactive_with_the_reason(fake: Path) -> None:
    host = await _host({"formatter": {"ghost": {}}})
    ghost = {s.name: s for s in host.statuses()}["ghost"]
    assert not ghost.enabled and not ghost.started  # inactive, not silently ignored
    assert "[plugins.formatter.ghost] is in config.toml" in ghost.detail and "no formatter plugin called 'ghost'" in ghost.detail and "settings are kept" in ghost.detail


async def test_adding_a_plugin_adopts_the_settings_table_that_was_waiting_for_it(fake: Path) -> None:
    host = await _host({"formatter": {"mine": {"x": 1}}})
    plan = host.plan_add("oaken_plugins.formatter_demo:DemoFormatter", kind="formatter", name="mine")
    assert plan.name == "mine" and plan.table["class"] == "oaken_plugins.formatter_demo:DemoFormatter" and plan.how == "reload"  # not "already exists"
    loaded = await _host({"formatter": {"demo": {"class": "x:Y"}}})
    with pytest.raises(Exception, match="already exists"):
        loaded.plan_add("oaken_plugins.formatter_demo:DemoFormatter", kind="formatter", name="demo")  # one that already has its class really does exist


async def test_kind_dot_name_says_both_at_once(fake: Path) -> None:
    host = await _host({})
    plan = host.plan_add("formatter.demo")
    assert (plan.kind, plan.name) == ("formatter", "demo") and plan.table["class"] == "oaken_plugins.formatter_demo:DemoFormatter"
    with pytest.raises(Exception, match="use one"):
        host.plan_add("formatter.demo", kind="embedder")
