# SPDX-License-Identifier: Apache-2.0
import tomllib
from pathlib import Path

import pytest

from oaken_core.errors import InvalidInput, PermissionDenied
from oaken_core.operations.caller import CallerContext

from .test_console import Rig, rig  # noqa: F401 - the rig fixture: a started core with a "notes" enricher

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
        ({"plugin": "ghost"}, "no usable enricher plugin named 'ghost'"),
        ({"name": "notes"}, "already exists"),
        ({"name": "Bad Name"}, "not a valid instance name"),
        ({"config": {"limit": "lots"}}, "limit"),  # the plugin's config_model refuses it
        ({"kind": "nope"}, "no plugin kind"),
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
