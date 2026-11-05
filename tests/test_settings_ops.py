# SPDX-License-Identifier: Apache-2.0
import tomllib
from pathlib import Path
from typing import Any

import pytest

from oaken_core.errors import InvalidInput, NotFound
from oaken_core.operations.caller import CallerContext

from .conftest import Ops

CLI = CallerContext.cli()


def cfg(home: Path) -> Path:
    return home / "config" / "config.toml"


async def call(ops: Ops, op: str, body: dict[str, Any], confirm: bool = True) -> Any:
    return await ops.client.call(op, body, CLI, confirm=confirm)


async def test_settings_get_lists_each_section_with_what_can_be_changed_here(ops: Ops) -> None:
    out = (await call(ops, "settings_get", {})).output
    names = [s.name for s in out.sections]
    assert names == ["log", "jobs", "swarm", "router", "console"]
    swarm = {r.key: r for s in out.sections if s.name == "swarm" for r in s.settings}
    assert swarm["max_agents"].editable and not swarm["enabled"].editable and "by hand" in swarm["enabled"].note
    one = (await call(ops, "settings_get", {"section": "jobs"})).output
    assert [s.name for s in one.sections] == ["jobs"]
    with pytest.raises(NotFound):
        await call(ops, "settings_get", {"section": "nope"})


async def test_settings_set_proposes_with_before_and_after_then_writes_one_table(ops: Ops, oaken_home: Path) -> None:
    cfg(oaken_home).parent.mkdir(parents=True)
    cfg(oaken_home).write_text('# my notes\n[plugins.enricher.notes]\ngreeting = "hi"\n\n[jobs]\nlease_seconds = 30\n')
    body = {"section": "jobs", "values": {"max_attempts": 7, "lease_seconds": 30.0}}
    proposal = (await call(ops, "settings_set", body, confirm=False)).proposal
    assert [(c.field, c.before, c.after) for c in proposal.changes] == [("max_attempts", 3, 7)]  # lease_seconds is unchanged
    assert "max_attempts" not in cfg(oaken_home).read_text()  # a proposal writes nothing
    out = (await call(ops, "settings_set", body)).output
    assert out.changed == ["max_attempts"] and out.effect == "reload" and "Reload core" in out.note
    text = cfg(oaken_home).read_text()
    assert "# my notes" in text and 'greeting = "hi"' in text  # the rest of the file is as it was
    assert tomllib.loads(text)["jobs"] == {"lease_seconds": 30, "max_attempts": 7}


async def test_settings_set_with_nothing_new_writes_nothing(ops: Ops, oaken_home: Path) -> None:
    out = (await call(ops, "settings_set", {"section": "jobs", "values": {"max_attempts": 3}})).output
    assert out.changed == [] and out.effect == "none" and not cfg(oaken_home).exists()


@pytest.mark.parametrize(
    ("section", "values", "why"),
    [
        ("swarm", {"enabled": True}, "cannot be changed here"),
        ("router", {"lan_allowlist": ["x"]}, "widens who may see confidential"),
        ("console", {"port": 9000}, "cannot be changed here"),
        ("jobs", {"nonsense": 1}, "not a setting of jobs"),
        ("jobs", {"max_attempts": 0}, "max_attempts"),  # the whole file has to load
        ("log", {"level": "LOUD"}, "level"),
    ],
)  # fmt: skip
async def test_settings_set_refuses_by_hand_keys_unknown_keys_and_values_the_file_could_not_load(
    ops: Ops, oaken_home: Path, section: str, values: dict[str, Any], why: str
) -> None:
    with pytest.raises(InvalidInput, match=why):
        await call(ops, "settings_set", {"section": section, "values": values})
    assert not cfg(oaken_home).exists()


async def test_settings_set_is_never_offered_over_mcp(ops: Ops) -> None:
    from oaken_core.errors import PermissionDenied

    with pytest.raises(PermissionDenied):
        await ops.client.call("settings_set", {"section": "jobs", "values": {"max_attempts": 5}}, CallerContext.mcp("t"))
