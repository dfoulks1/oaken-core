# SPDX-License-Identifier: Apache-2.0
import tomllib
from pathlib import Path

import pytest

from oaken_core.errors import InvalidInput, PermissionDenied
from oaken_core.operations.caller import CallerContext
from oaken_core.plugins import extensions

from .test_console import Rig, make_core, rig  # noqa: F401 - the rig fixture: a started core with a "notes" enricher

CLI = CallerContext.cli()


async def add(r: Rig, confirm: bool = True, **over: object) -> object:
    body = {"kind": "enricher", "name": "desk", "plugin": "notes", "config": {"greeting": "yo", "limit": 5}, **over}
    return await r.core.client.call("plugin_add", body, CLI, confirm=confirm)


async def test_plugin_add_proposes_first_then_checks_writes_and_starts_the_instance(rig: Rig, oaken_home: Path) -> None:  # noqa: F811
    proposal = await add(rig, confirm=False)
    assert proposal.proposal is not None and "desk" in proposal.proposal.summary  # type: ignore[attr-defined]
    assert {c.field for c in proposal.proposal.changes} == {"plugin", "greeting", "limit"}  # type: ignore[attr-defined]
    assert "yo" not in str(proposal.proposal)  # names only, never values  # type: ignore[attr-defined]
    assert not (oaken_home / "config" / "config.toml").exists()  # a proposal changes nothing
    done = await add(rig)
    assert done.output.started and done.output.name == "desk"  # type: ignore[attr-defined]
    table = tomllib.loads((oaken_home / "config" / "config.toml").read_text())["plugins"]["enricher"]["desk"]
    assert table == {"plugin": "notes", "greeting": "yo", "limit": 5}
    (status,) = [s for s in rig.core.host.statuses() if s.name == "desk"]
    assert status.started and status.plugin == "notes"


@pytest.mark.parametrize(
    ("over", "why"),
    [
        ({"plugin": "ghost"}, "no plugin named 'ghost'"),
        ({"name": "desk", "plugin": "pkg.nothing.Here"}, "cannot import"),
        ({"name": "Bad Name"}, "not a valid instance name"),
        ({"config": {"limit": "lots"}}, "limit"),  # the plugin's config_model refuses it
        ({"kind": "nope"}, "no plugin kind"),
        ({"plugin": "tests.test_plugin_add.NotAPlugin", "kind": None}, "does not say what kind"),
    ],
)  # fmt: skip
async def test_plugin_add_refuses_a_bad_request_before_writing_anything(rig: Rig, oaken_home: Path, over: dict, why: str) -> None:  # noqa: F811
    with pytest.raises(Exception, match=why):
        await add(rig, **over)
    assert not (oaken_home / "config" / "config.toml").exists()


async def test_plugin_add_refuses_a_secret_and_is_never_offered_over_mcp(rig: Rig, oaken_home: Path) -> None:  # noqa: F811
    with pytest.raises(Exception, match="secret|keyring|password"):
        await add(rig, config={"password": "hunter2"})
    assert not any(s.name == "desk" for s in rig.core.host.statuses())
    with pytest.raises(PermissionDenied):
        await rig.core.client.call("plugin_add", {"kind": "enricher", "name": "x", "plugin": "notes"}, CallerContext.mcp("t"))


class NotAPlugin:
    """A class that names no plugin kind."""


async def test_plugin_add_by_name_switches_a_loaded_plugin_on_and_it_persists(rig: Rig, oaken_home: Path) -> None:  # noqa: F811
    await rig.core.client.call("plugin_disable", {"plugin": "enricher.notes"}, CLI, confirm=True)
    done = await rig.core.client.call("plugin_add", {"plugin": "notes"}, CLI, confirm=True)  # no kind, no name: the plugin itself
    assert done.output.started and done.output.name == "notes" and not done.output.reloading  # type: ignore[union-attr]
    assert tomllib.loads((oaken_home / "config" / "config.toml").read_text())["plugins"]["enricher"]["notes"] == {"enabled": True}


async def test_plugin_add_an_unloaded_plugin_in_an_extensions_directory_writes_the_config_and_reloads(rig: Rig, oaken_home: Path, tmp_path: Path) -> None:  # noqa: F811
    plugin = tmp_path / "oaken-enricher-shout"
    (plugin / "src" / "oaken_enricher_shout").mkdir(parents=True)
    (plugin / "pyproject.toml").write_text('[project]\nname = "oaken-enricher-shout"\n[project.entry-points."oaken_core.enrichers"]\nshout = "oaken_enricher_shout:Shout"\n[project.entry-points."oaken.providers"]\nmail = "x:Y"\n')
    (plugin / "src" / "oaken_enricher_shout" / "__init__.py").write_text("from oaken_core.plugins.base import BaseEnricher\nclass Shout(BaseEnricher):\n    version = '1'\n    needs_llm = False\n    async def enrich(self, target):\n        return None\n")
    host = rig.core.host
    host.extensions_dirs = (str(tmp_path),)
    reloads: list[int] = []
    rig.core.client.svc.reload = lambda: reloads.append(1)
    ask = await rig.core.client.call("plugin_add", {"plugin": "oaken-enricher-shout"}, CLI)  # a directory's name
    assert ask.proposal is not None and "reload core" in ask.proposal.summary and not (oaken_home / "config" / "config.toml").exists()
    done = await rig.core.client.call("plugin_add", {"plugin": "oaken-enricher-shout"}, CLI, confirm=True)
    assert done.output.reloading and reloads == [1]  # type: ignore[union-attr]
    assert tomllib.loads((oaken_home / "config" / "config.toml").read_text())["plugins"]["enricher"]["shout"] == {"enabled": True}
    assert [ep.name for ep in extensions.entry_points(host.extensions_dirs, "oaken_core.enrichers")] == ["shout"]
    assert extensions.entry_points(host.extensions_dirs, "oaken.providers") == []  # a plugin made for oaken-mail is not ours to load


async def test_plugin_add_a_class_path_persists_as_a_class_and_the_host_loads_it_from_there(rig: Rig, oaken_home: Path) -> None:  # noqa: F811
    reloads: list[int] = []
    rig.core.client.svc.reload = lambda: reloads.append(1)
    done = await rig.core.client.call("plugin_add", {"plugin": "tests.test_console.Notes", "name": "again", "kind": "enricher"}, CLI, confirm=True)
    assert done.output.reloading and reloads == [1]  # type: ignore[union-attr]
    assert tomllib.loads((oaken_home / "config" / "config.toml").read_text())["plugins"]["enricher"]["again"] == {"class": "tests.test_console:Notes"}


async def test_a_table_with_a_class_is_a_plugin_the_host_loads_at_start(tmp_path: Path, oaken_home: Path) -> None:
    core = await make_core(tmp_path, {"again": {"class": "tests.test_console:Notes"}})
    try:
        (status,) = [s for s in core.host.statuses() if s.name == "again"]
        assert status.started and status.loadpath == "tests.test_console:Notes"
    finally:
        await core.host.stop()


async def test_the_host_finds_plugins_in_extensions_directories_and_skips_a_world_writable_one(tmp_path: Path) -> None:
    good, bad = tmp_path / "good", tmp_path / "bad"
    for root, wide in ((good, False), (bad, True)):
        (root / "oaken-enricher-x").mkdir(parents=True)
        (root / "oaken-enricher-x" / "pyproject.toml").write_text('[project.entry-points."oaken_core.enrichers"]\nx = "m:X"\n')
        root.chmod(0o777 if wide else 0o755)
    assert [e.name for e in extensions.entry_points([str(good), str(bad), str(tmp_path / "missing")], "oaken_core.enrichers")] == ["x"]


async def test_every_plugin_operation_names_its_target_as_kind_dot_name(rig: Rig, oaken_home: Path) -> None:  # noqa: F811
    call = rig.core.client.call
    await add(rig)
    assert (await call("plugin_disable", {"plugin": "enricher.desk"}, CLI, confirm=True)).output.affected == ["desk"]
    assert (await call("plugin_enable", {"plugin": "enricher.desk"}, CLI, confirm=True)).output.affected == ["desk"]
    assert (await call("plugin_reload", {"plugin": "enricher.desk"}, CLI, confirm=True)).output.affected == ["desk"]
    assert (await call("plugin_test", {"plugin": "enricher.desk"}, CLI)).output.ok
    assert [p.name for p in (await call("plugin_describe", {"plugin": "enricher.desk"}, CLI)).output.plugins] == ["desk"]
    for op in ("plugin_enable", "plugin_remove", "plugin_test", "plugin_describe"):
        with pytest.raises(InvalidInput, match="KIND.NAME"):
            await call(op, {"plugin": "desk"}, CLI, confirm=True)  # a bare name is refused: it could be two kinds
    assert (await call("plugin_remove", {"plugin": "enricher.desk"}, CLI, confirm=True)).output.name == "desk"
    assert "desk" not in {s.name for s in rig.core.host.statuses()}
    assert "desk" not in tomllib.loads((oaken_home / "config" / "config.toml").read_text()).get("plugins", {}).get("enricher", {})


def test_a_plugin_comes_from_core_plugins_or_local(tmp_path: Path) -> None:
    src = tmp_path / "oaken-x" / "src" / "mine_pkg"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (tmp_path / "oaken-x" / "pyproject.toml").write_text("")
    import sys

    sys.path.append(str(tmp_path / "oaken-x" / "src"))
    try:
        assert extensions.origin("oaken_core.triage.rules:RulesTriager") == "core"
        assert extensions.origin("mine_pkg:Thing") == "local"  # on this disk, outside site-packages
        assert extensions.origin("pydantic:BaseModel") == "plugins"  # something installed from an index
    finally:
        sys.path.remove(str(tmp_path / "oaken-x" / "src"))


async def test_plugin_list_shows_the_api_each_plugin_speaks_and_status_the_one_core_speaks(rig: Rig) -> None:
    from oaken_core.plugins.contracts import PLUGIN_API_VERSION

    rows = (await rig.core.client.call("plugin_list", {}, CLI)).output.plugins
    assert rows and {r.api for r in rows if r.id == "enricher.notes"} == {PLUGIN_API_VERSION}
    assert (await rig.core.client.call("status", {}, CLI)).output.plugin_api == PLUGIN_API_VERSION


def test_every_core_plugin_says_its_api_and_the_core_it_needs() -> None:
    from importlib.metadata import entry_points

    from oaken_core.plugins.contracts import PLUGIN_API_VERSION

    eps = [e for g in ("stores", "vector_stores", "ingestors", "providers", "triagers", "formatters") for e in entry_points(group=f"oaken_core.{g}") if e.value.startswith("oaken_core.")]
    assert len(eps) >= 9
    for ep in eps:
        cls = ep.load()
        assert cls.__dict__.get("api_version") == PLUGIN_API_VERSION and cls.__dict__.get("requires_core"), ep.value  # written in the class, so `oaken plugins` can read it without importing


async def test_plugin_remove_refuses_a_plugin_with_no_class_table(rig: Rig, oaken_home: Path) -> None:  # noqa: F811
    host = rig.core.client.svc.host
    with pytest.raises(Exception, match="switch it off"):
        await host.remove_instance("enricher", "notes")  # a plugin with no class table: switched off, not removed
