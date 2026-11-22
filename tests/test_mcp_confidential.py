# SPDX-License-Identifier: Apache-2.0
"""The confidential gate on the MCP doors: an MCP client gets nothing from an item labelled
confidential unless its door was cleared for that. Synthetic items only."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from oaken_core.actors import USER
from oaken_core.api.mcp_server import McpServer
from oaken_core.daemon.core import Core
from oaken_core.errors import ConfidentialWithheld, NotExposed
from oaken_core.operations.caller import CallerContext
from oaken_core.plugins.base import BasePlugin, BaseProvider, command
from oaken_core.plugins.contracts import PluginOpContext
from oaken_core.settings import ApiSettings, Settings

from .conftest import Ops
from .test_host import EP
from .test_items import make

CLI = CallerContext.cli()
SHUT = CallerContext.mcp("cloud-assistant")  # a client whose door has not cleared it
CLEARED = CallerContext.mcp("local-model", confidential=True)


# -- who may see confidential items -----------------------------------------------------------
def test_the_person_and_core_may_see_confidential_items_and_an_mcp_client_may_not_unless_cleared() -> None:
    assert CallerContext.cli().may_see_confidential and CallerContext.ui().may_see_confidential
    assert CallerContext.internal().may_see_confidential
    assert not CallerContext.mcp("x").may_see_confidential  # the default for MCP is no
    assert not CallerContext.mcp("x", agent_id="a1").may_see_confidential
    assert CallerContext.mcp("x", confidential=True).may_see_confidential


def test_the_person_door_is_not_cleared_unless_the_config_says_so() -> None:
    assert ApiSettings().mcp_confidential is False
    assert Settings.model_validate({"api": {"mcp_confidential": True}}).api.mcp_confidential is True


async def test_the_person_door_clears_its_caller_only_from_the_setting(ops: Ops) -> None:
    assert not McpServer(ops.client, token="t")._caller().may_see_confidential
    ops.svc.settings = Settings(swarm=ops.svc.settings.swarm, version="9.9.9", api=ApiSettings(mcp_confidential=True))
    assert McpServer(ops.client, token="t")._caller().may_see_confidential


# -- core's item operations -------------------------------------------------------------------
@pytest.fixture
async def two_items(tmp_path: Path) -> AsyncIterator[tuple[Core, str, str]]:
    """A core holding one plain item and one labelled confidential, with item_show and item_get
    turned on over MCP (they are off by default)."""
    core = await make(tmp_path)
    call = core.client.call
    try:
        await call("intake_submit", {"source": "notes", "body": "plain note"}, CLI, confirm=True)
        await call("intake_submit", {"source": "notes", "body": "board minutes", "labels": {"confidential": "true"}}, CLI, confirm=True)
        await core.runner.drain()
        items = (await call("items.list", {}, CLI)).output.items  # type: ignore[union-attr]
        by_body = {i.preview: i.subject_id for i in items}
        for name in ("item_show", "item_get", "log_list", "label_set"):
            await core.client.exposure.set(f"op:{name}", True, actor=USER)
        yield core, by_body["plain note"], by_body["board minutes"]
    finally:
        await core.host.stop()


async def test_item_show_and_item_get_withhold_a_confidential_item_from_an_uncleared_mcp_client(two_items: tuple[Core, str, str]) -> None:
    core, plain, secret = two_items
    call = core.client.call
    assert (await call("item_show", {"id": plain}, SHUT)).output.parts == {"body": "plain note"}  # type: ignore[union-attr]
    for op, args in (("item_show", {"id": secret}), ("item_get", {"id": secret, "name": "body"}),
                     ("item_get", {"id": secret, "name": "labels.confidential"})):  # fmt: skip
        with pytest.raises(ConfidentialWithheld):
            await call(op, args, SHUT)


async def test_a_cleared_mcp_client_and_the_person_read_a_confidential_item(two_items: tuple[Core, str, str]) -> None:
    core, _, secret = two_items
    for caller in (CLEARED, CLI):
        shown = (await core.client.call("item_show", {"id": secret}, caller)).output
        assert shown.parts == {"body": "board minutes"} and shown.labels["confidential"] == "true"  # type: ignore[union-attr]


async def test_label_get_label_find_and_log_list_leave_a_confidential_item_out_for_an_uncleared_mcp_client(two_items: tuple[Core, str, str]) -> None:
    core, plain, secret = two_items
    call = core.client.call
    with pytest.raises(ConfidentialWithheld):
        await call("label_get", {"subject_id": secret}, SHUT)  # on by default over MCP, so this mattered most
    assert (await call("label_get", {"subject_id": plain}, SHUT)).output.labels  # type: ignore[union-attr]
    found = lambda caller: call("label_find", {"key": "format"}, caller)
    assert {h.subject_id for h in (await found(SHUT)).output.hits} == {plain}  # type: ignore[union-attr]
    assert {h.subject_id for h in (await found(CLEARED)).output.hits} == {plain, secret}  # type: ignore[union-attr]
    with pytest.raises(ConfidentialWithheld):  # a proposal would show the value it replaces
        await call("label_set", {"subject_id": secret, "key": "format", "value": "none"}, SHUT)
    assert (await call("label_set", {"subject_id": secret, "key": "format", "value": "none"}, CLEARED)).proposal is not None
    logged = lambda caller: call("log_list", {"limit": 500}, caller)
    assert secret not in {r.subject_id for r in (await logged(SHUT)).output.rows}  # type: ignore[union-attr]
    assert secret in {r.subject_id for r in (await logged(CLEARED)).output.rows}  # type: ignore[union-attr]


# -- plugin operations: offered to an uncleared client only if they honour the gate ------------
class EchoIn(BaseModel):
    subject_id: str


class EchoOut(BaseModel):
    cleared: bool


class Reader(BaseProvider):
    """Two commands: one that says nothing about the gate, one that promises to honour it."""

    @command("dump", "Says nothing about confidential items.", input=EchoIn, output=EchoOut)
    async def dump(self, ctx: PluginOpContext, inp: EchoIn) -> EchoOut:
        return EchoOut(cleared=ctx.confidential)

    @command("careful", "Leaves confidential items out when it must.", input=EchoIn, output=EchoOut, confidential_aware=True)
    async def careful(self, ctx: PluginOpContext, inp: EchoIn) -> EchoOut:
        return EchoOut(cleared=ctx.confidential)


@pytest.fixture
async def reader(tmp_path: Path) -> AsyncIterator[Core]:
    core = await make(tmp_path, {"oaken_core.providers": [EP("reader", Reader)]})
    try:
        for name in ("reader.dump", "reader.careful"):
            await core.client.exposure.set(f"op:{name}", True, actor=USER)
        yield core
    finally:
        await core.host.stop()


async def test_an_uncleared_mcp_client_is_offered_only_the_plugin_commands_that_honour_the_gate(reader: Core) -> None:
    client = reader.client
    shut = {o.name for o in await client.visible(SHUT)}
    assert "reader.careful" in shut and "reader.dump" not in shut
    assert {"reader.careful", "reader.dump"} <= {o.name for o in await client.visible(CLEARED)}
    with pytest.raises(NotExposed, match="not available to MCP clients"):  # the same words as switched off
        await client.call("reader.dump", {"subject_id": "s"}, SHUT)


async def test_a_plugin_command_is_told_whether_its_caller_may_see_confidential_items(reader: Core) -> None:
    async def cleared(op: str, caller: CallerContext) -> Any:
        return (await reader.client.call(op, {"subject_id": "s"}, caller)).output.cleared  # type: ignore[union-attr]

    assert await cleared("reader.careful", SHUT) is False
    assert await cleared("reader.careful", CLEARED) is True
    assert await cleared("reader.dump", CLI) is True and await cleared("reader.dump", CLEARED) is True


def test_a_command_says_nothing_about_the_gate_unless_it_declares_it() -> None:
    ops = {o.name: o for o in Reader().operations()}
    assert ops["dump"].confidential_aware is False and ops["careful"].confidential_aware is True
    assert BasePlugin().operations() == []


# -- the agent door: the agent's locality, which only the person sets ---------------------------
async def test_the_agent_door_clears_only_an_agent_the_person_said_runs_on_this_machine(ops: Ops) -> None:
    from oaken_core.api import mcp_server

    door = McpServer(ops.client, agents=ops.agents)
    for locality, cleared in (("local", True), ("lan", False), ("hosted", False)):
        agent, _ = await ops.agents.create(f"a-{locality}", ["work"], locality=locality)
        reset = mcp_server._AGENT.set(agent)
        try:
            caller = door._caller()
        finally:
            mcp_server._AGENT.reset(reset)
        assert caller.may_see_confidential is cleared and caller.agent_id == str(agent.id)
    default, _ = await ops.agents.create("unsaid", ["work"])
    assert not default.may_see_confidential  # an agent nobody placed is off the machine
