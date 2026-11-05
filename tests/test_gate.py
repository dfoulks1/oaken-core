# SPDX-License-Identifier: Apache-2.0
"""The confirm gate: a person confirms their own action; an agent, only by a proposal token."""

import pytest
from pydantic import BaseModel

from oaken_core.actors import SYSTEM, USER
from oaken_core.clock import FakeClock
from oaken_core.errors import (
    ConfirmationInvalid,
    InvalidInput,
    NotExposed,
    NotFound,
    QuotaExceeded,
    SendDisabled,
)
from oaken_core.operations.caller import CallerContext
from oaken_core.operations.gate import Binding, InMemoryProposals, hash_input

from .conftest import Ops
from .test_operations import CLI, mcp_agent
from .test_registry import define

UI = CallerContext.ui()


async def paused(ops: Ops) -> bool:
    return (await ops.kit.queue.stats())[0].paused


async def agent_with_pause(ops: Ops) -> CallerContext:
    for name in ("queue_pause", "queue_resume"):
        await ops.client.exposure.set(f"op:{name}", True, actor=USER)  # the owner turns it on
    return await mcp_agent(ops, "queue_pause", "queue_resume")


async def test_an_agent_calling_a_mutation_gets_a_proposal_and_nothing_changes(ops: Ops) -> None:
    who = await agent_with_pause(ops)
    result = await ops.client.call("queue_pause", {"queue": "default"}, who)
    assert result.output is None and result.proposal is not None
    p = result.proposal
    assert p.operation == "queue_pause" and p.total_changes == 1
    assert [(c.target, c.field, c.after) for c in p.changes] == [("default", "paused", True)]
    assert p.confirmation_token.startswith("oaken_confirm_") and p.expires_at > ops.kit.clock.now()
    assert not await paused(ops)  # no side effects


async def test_presenting_the_token_runs_it_once_and_a_replay_fails(ops: Ops) -> None:
    who = await agent_with_pause(ops)
    p = (await ops.client.call("queue_pause", {"queue": "default"}, who)).proposal
    assert p is not None
    done = await ops.client.call("queue_pause", {"queue": "default"}, who, confirmation_token=p.confirmation_token)
    assert done.executed and await paused(ops)
    with pytest.raises(ConfirmationInvalid):  # single use
        await ops.client.call("queue_pause", {"queue": "default"}, who, confirmation_token=p.confirmation_token)


async def test_changing_the_input_after_the_proposal_fails_and_burns_the_token(ops: Ops) -> None:
    who = await agent_with_pause(ops)
    p = (await ops.client.call("queue_pause", {"queue": "default"}, who)).proposal
    assert p is not None
    with pytest.raises(ConfirmationInvalid, match="different call"):
        await ops.client.call("queue_pause", {"queue": "other"}, who, confirmation_token=p.confirmation_token)
    with pytest.raises(ConfirmationInvalid):  # the right input no longer works either
        await ops.client.call("queue_pause", {"queue": "default"}, who, confirmation_token=p.confirmation_token)
    assert not await paused(ops)


async def test_a_token_is_for_one_operation_and_one_agent(ops: Ops) -> None:
    one = await agent_with_pause(ops)
    two = await agent_with_pause(ops)
    p = (await ops.client.call("queue_pause", {"queue": "default"}, one)).proposal
    assert p is not None
    with pytest.raises(ConfirmationInvalid):  # another agent cannot use it
        await ops.client.call("queue_pause", {"queue": "default"}, two, confirmation_token=p.confirmation_token)
    q = (await ops.client.call("queue_pause", {"queue": "default"}, one)).proposal
    assert q is not None
    with pytest.raises(ConfirmationInvalid):  # nor another operation
        await ops.client.call("queue_resume", {"queue": "default"}, one, confirmation_token=q.confirmation_token)
    assert not await paused(ops)


async def test_an_expired_token_fails(ops: Ops) -> None:
    who = await agent_with_pause(ops)
    p = (await ops.client.call("queue_pause", {"queue": "default"}, who)).proposal
    assert p is not None
    ops.kit.clock.advance(seconds=ops.svc.settings.operations.confirm_ttl_seconds + 1)
    with pytest.raises(ConfirmationInvalid, match="expired"):
        await ops.client.call("queue_pause", {"queue": "default"}, who, confirmation_token=p.confirmation_token)
    assert not await paused(ops)


async def test_an_agent_cannot_confirm_itself_by_saying_so(ops: Ops) -> None:
    who = await agent_with_pause(ops)
    result = await ops.client.call("queue_pause", {"queue": "default"}, who, confirm=True)  # ignored
    assert result.proposal is not None and result.output is None
    assert not await paused(ops)


async def test_a_person_at_the_cli_or_ui_confirms_their_own_action(ops: Ops) -> None:
    for caller, name, want in ((CLI, "queue_pause", True), (UI, "queue_resume", False)):
        done = await ops.client.call(name, {"queue": "default"}, caller, confirm=True)
        assert done.output is not None and done.executed and await paused(ops) is want
    # without confirming they get the same proposal an agent would, to show and ask about
    asked = await ops.client.call("queue_pause", {"queue": "default"}, CLI)
    assert asked.proposal is not None and not await paused(ops)
    # and a person may confirm with the token too
    go = await ops.client.call("queue_pause", {"queue": "default"}, CLI, confirmation_token=asked.proposal.confirmation_token)
    assert go.output is not None and await paused(ops)


def test_two_spellings_of_one_call_are_one_call() -> None:
    class Two(BaseModel):
        queue: str
        note: str = ""

    assert hash_input("op", Two(queue="a")) == hash_input("op", Two(queue="a", note=""))  # defaults applied
    assert hash_input("op", Two(queue="a")) != hash_input("op", Two(queue="b"))
    assert hash_input("op", Two(queue="a")) != hash_input("other", Two(queue="a"))


async def test_a_read_needs_no_confirmation_and_a_stray_token_is_ignored(ops: Ops) -> None:
    who = CallerContext.mcp("claude")
    result = await ops.client.call("status", None, who, confirmation_token="oaken_confirm_nonsense")
    assert result.output is not None and result.executed


async def test_sending_is_refused_for_everyone_even_when_defined(ops: Ops) -> None:
    ops.registry.register(define("send_message", permission="send", mcp="never"))
    for caller in (CLI, UI, CallerContext.internal()):
        with pytest.raises(SendDisabled):
            await ops.client.call("send_message", {}, caller, confirm=True)
    with pytest.raises(NotExposed):  # an agent does not even get as far as the gate
        await ops.client.call("send_message", {}, CallerContext.mcp("claude"), confirm=True)


async def test_unknown_operations_and_bad_input_are_plain_errors(ops: Ops) -> None:
    with pytest.raises(NotFound):
        await ops.client.call("no_such_thing", None, CLI)
    with pytest.raises(InvalidInput) as e:
        await ops.client.call("queue_pause", {"queue": ["secret-value"]}, CLI)
    assert "secret-value" not in str(e.value)  # where and what, never the value
    with pytest.raises(InvalidInput):
        await ops.client.call("queue_pause", {}, CLI)


async def test_an_mcp_caller_is_always_an_agent_and_a_cli_caller_always_the_user() -> None:
    with pytest.raises(ValueError, match="agent"):
        CallerContext("mcp", SYSTEM)
    with pytest.raises(ValueError, match="user"):
        CallerContext("cli", SYSTEM)
    a, b = CallerContext.mcp("a"), CallerContext.mcp("b")
    assert a.key != b.key and a.key != CLI.key


async def test_an_operation_that_is_off_over_mcp_looks_like_one_that_does_not_exist_for_it(ops: Ops) -> None:
    who = CallerContext.mcp("claude")
    with pytest.raises(NotExposed) as never:
        await ops.client.call("agent_create", {"name": "x"}, who)  # never, by code
    with pytest.raises(NotExposed) as off:
        await ops.client.call("queue_pause", {"queue": "default"}, who)  # exists, off by default
    assert type(never.value) is type(off.value)
    assert "not available to MCP clients" in never.value.message
    assert "not available to MCP clients" in off.value.message


def test_the_proposal_store_caps_what_one_caller_can_hold() -> None:
    clock = FakeClock()
    store = InMemoryProposals(clock, ttl_seconds=60, max_per_caller=2)
    mine = Binding("op", "h", "mcp:agent:a")
    store.issue(mine)
    store.issue(mine)
    with pytest.raises(QuotaExceeded):
        store.issue(mine)
    store.issue(Binding("op", "h", "mcp:agent:b"))  # another caller is not affected
    clock.advance(seconds=61)
    store.issue(mine)  # the old ones expired


def test_operation_dataclass_is_frozen() -> None:
    op = define()
    with pytest.raises(AttributeError):
        op.name = "other"  # type: ignore[misc]
