# SPDX-License-Identifier: Apache-2.0
"""``oaken-plugins``: one distribution, a subpackage per plugin, nothing imported until it is added."""

import sys
from pathlib import Path

import pytest

from oaken_core.cli.commands import main
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
    assert "demo" in out and "Splits demo text" in out and "plugin add NAME" in out
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
