# SPDX-License-Identifier: Apache-2.0
"""The confirm gate: a person confirms their own action; an agent, only by a proposal token."""

import pytest

from oaken_core.errors import (
    ConfirmationInvalid,
    InvalidInput,
    NotExposed,
    NotFound,
    QuotaExceeded,
    SendDisabled,
)
from oaken_core.operations import CallerContext, Operation
from oaken_core.operations.gate import InMemoryProposals
from oaken_core.operations.tests.conftest import CLI, UI, Ops, agent
from oaken_core.operations.tests.test_registry import define


async def test_an_agent_calling_a_mutation_gets_a_proposal_and_nothing_changes(ops: Ops) -> None:
    ids = await ops.add(3)
    before = await ops.svc.audit.recent(100)
    result = await ops.call("mark_read", {"ids": ids[:2]}, agent(), confirm=False)
    assert result.output is None and result.proposal is not None
    p = result.proposal
    assert p.operation == "mark_read" and p.total_changes == 2
    assert {(c.target, c.field, c.before, c.after) for c in p.changes} == {
        (ids[0], "is_read", False, True),
        (ids[1], "is_read", False, True),
    }
    assert p.confirmation_token.startswith("oaken_confirm_") and p.expires_at > ops.rig.clock.now()
    assert await ops.read_count() == 0  # no side effects
    assert await ops.svc.audit.recent(100) == before
    assert ops.rs.writes() == []


async def test_presenting_the_token_runs_it_once_and_a_replay_fails(ops: Ops) -> None:
    ids = await ops.add(2)
    who = agent()
    p = (await ops.call("mark_read", {"ids": ids}, who, confirm=False)).proposal
    assert p is not None
    done = await ops.call("mark_read", {"ids": ids}, who, confirm=False, token=p.confirmation_token)
    assert done.output is not None and sorted(done.output.changed) == sorted(ids)  # type: ignore[attr-defined]
    assert await ops.read_count() == 2
    with pytest.raises(ConfirmationInvalid):  # single use
        await ops.call("mark_read", {"ids": ids}, who, confirm=False, token=p.confirmation_token)


async def test_changing_the_input_after_the_proposal_fails_and_burns_the_token(ops: Ops) -> None:
    a, b, c = await ops.add(3)
    who = agent()
    p = (await ops.call("mark_read", {"ids": [a, b]}, who, confirm=False)).proposal
    assert p is not None
    with pytest.raises(ConfirmationInvalid, match="different call"):
        await ops.call(
            "mark_read", {"ids": [a, b, c]}, who, confirm=False, token=p.confirmation_token
        )
    with pytest.raises(ConfirmationInvalid):  # the right input no longer works either
        await ops.call("mark_read", {"ids": [a, b]}, who, confirm=False, token=p.confirmation_token)
    assert await ops.read_count() == 0


async def test_a_token_is_for_one_operation_and_one_agent(ops: Ops) -> None:
    (a,) = await ops.add(1)
    p = (await ops.call("mark_read", {"ids": [a]}, agent("one"), confirm=False)).proposal
    assert p is not None
    with pytest.raises(ConfirmationInvalid):  # another agent cannot use it
        await ops.call(
            "mark_read", {"ids": [a]}, agent("two"), confirm=False, token=p.confirmation_token
        )
    q = (await ops.call("mark_read", {"ids": [a]}, agent("one"), confirm=False)).proposal
    assert q is not None
    with pytest.raises(ConfirmationInvalid):  # nor another operation
        await ops.call(
            "mark_unread", {"ids": [a]}, agent("one"), confirm=False, token=q.confirmation_token
        )
    assert await ops.read_count() == 0


async def test_an_expired_token_fails(ops: Ops) -> None:
    (a,) = await ops.add(1)
    p = (await ops.call("mark_read", {"ids": [a]}, agent(), confirm=False)).proposal
    assert p is not None
    ops.rig.clock.advance(seconds=ops.svc.settings.operations.confirm_ttl_seconds + 1)
    with pytest.raises(ConfirmationInvalid, match="expired"):
        await ops.call(
            "mark_read", {"ids": [a]}, agent(), confirm=False, token=p.confirmation_token
        )
    assert await ops.read_count() == 0


async def test_an_agent_cannot_confirm_itself_by_saying_so(ops: Ops) -> None:
    (a,) = await ops.add(1)
    result = await ops.call("mark_read", {"ids": [a]}, agent(), confirm=True)  # ignored
    assert result.proposal is not None and result.output is None
    assert await ops.read_count() == 0


async def test_a_person_at_the_cli_or_ui_confirms_their_own_action(ops: Ops) -> None:
    a, b = await ops.add(2)
    for caller, mid in ((CLI, a), (UI, b)):
        done = await ops.call("mark_read", {"ids": [mid]}, caller, confirm=True)
        assert done.output is not None and done.executed
    assert await ops.read_count() == 2
    # without confirming they get the same proposal an agent would, to show and ask about
    (c,) = await ops.add(1, start=2)
    asked = await ops.call("mark_read", {"ids": [c]}, CLI, confirm=False)
    assert asked.proposal is not None and not await ops.rs.is_read(c)
    # and a person may confirm with the token too
    go = await ops.call(
        "mark_read", {"ids": [c]}, CLI, confirm=False, token=asked.proposal.confirmation_token
    )
    assert go.output is not None and await ops.rs.is_read(c)


async def test_two_spellings_of_one_call_are_one_call(ops: Ops) -> None:
    (a,) = await ops.add(1)
    p = (await ops.call("mark_read", {"ids": [a]}, agent(), confirm=False)).proposal
    assert p is not None
    # the default for after_summary is spelled out: the validated input is the same
    done = await ops.call(
        "mark_read", {"ids": [a], "after_summary": False}, agent(), confirm=False,
        token=p.confirmation_token,
    )  # fmt: skip
    assert done.output is not None


async def test_a_read_needs_no_confirmation_and_a_stray_token_is_ignored(ops: Ops) -> None:
    result = await ops.call("status", None, agent(), confirm=False, token="oaken_confirm_nonsense")
    assert result.output is not None and result.executed


async def test_sending_is_refused_for_everyone_even_when_defined(ops: Ops) -> None:
    ops.registry.register(define("send_message", permission="send", mcp="never"))
    for caller in (CLI, UI, CallerContext.internal()):
        with pytest.raises(SendDisabled):
            await ops.call("send_message", {}, caller, confirm=True)
    with pytest.raises(NotExposed):  # an agent does not even get as far as the gate
        await ops.call("send_message", {}, agent(), confirm=True)


async def test_unknown_operations_and_bad_input_are_plain_errors(ops: Ops) -> None:
    with pytest.raises(NotFound):
        await ops.call("no_such_thing", None)
    with pytest.raises(InvalidInput) as e:
        await ops.call("mark_read", {"ids": "not-a-list"})
    assert "not-a-list" not in str(e.value)  # where and what, never the value
    with pytest.raises(InvalidInput):
        await ops.call("mark_read", {"ids": []})
    with pytest.raises(InvalidInput):
        await ops.call("mark_read", {})


async def test_an_mcp_caller_is_always_an_agent_and_a_cli_caller_always_the_user() -> None:
    from oaken_core.actors import SYSTEM

    with pytest.raises(ValueError, match="agent"):
        CallerContext("mcp", SYSTEM)
    with pytest.raises(ValueError, match="user"):
        CallerContext("cli", SYSTEM)
    assert agent("a").key != agent("b").key and agent("a").key != CLI.key


async def test_an_operation_that_is_off_over_mcp_looks_like_one_that_does_not_exist_for_it(
    ops: Ops,
) -> None:
    with pytest.raises(NotExposed) as never:
        await ops.call("get_raw", {"message_id": "x"}, agent())
    with pytest.raises(NotExposed) as off:
        await ops.call("list_workspaces", {}, agent())  # exists, off by default
    assert type(never.value) is type(off.value)
    assert "not available to MCP clients" in never.value.message


def test_the_proposal_store_caps_what_one_caller_can_hold() -> None:
    from oaken_core.clock import FakeClock
    from oaken_core.operations.gate import Binding

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
    assert isinstance(op, Operation)
    with pytest.raises(AttributeError):
        op.name = "other"  # type: ignore[misc]
