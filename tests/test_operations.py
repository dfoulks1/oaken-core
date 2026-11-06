# SPDX-License-Identifier: Apache-2.0
import json

import pytest
from pydantic import BaseModel

from oaken_core.actors import USER
from oaken_core.errors import Conflict, InvalidInput, NotExposed, PermissionDenied
from oaken_core.operations.caller import CallerContext
from oaken_core.operations.registry import Operation, OperationDefinitionError

from .conftest import Ops
from .test_jobs import Ping, Pong

CLI = CallerContext.cli()


async def mcp_agent(ops: Ops, *grants: str, kinds: tuple[str, ...] = ("work",)) -> CallerContext:
    agent, _ = await ops.agents.create("claude", list(kinds), operations=list(grants))
    return CallerContext.mcp("claude", agent_id=agent.id)


async def test_the_cli_sees_everything_and_a_person_over_mcp_only_what_is_on(ops: Ops) -> None:
    every = {o.name for o in await ops.client.visible(CLI)}
    mcp = {o.name for o in await ops.client.visible(CallerContext.mcp("me"))}
    assert {"queue_requeue", "agent_create", "label_clear"} <= every
    assert mcp == {"status", "queue_list", "queue_get", "job_list", "label_get", "label_keys", "label_find", "plugin_list", "plugin_describe", "job_claim",
                   "job_heartbeat", "job_complete", "job_fail"}  # fmt: skip
    assert not mcp & {o.name for o in ops.registry.all() if o.mcp == "never"}


async def test_a_mutation_is_a_proposal_until_a_person_confirms(ops: Ops) -> None:
    res = await ops.client.call("queue_pause", {"queue": "default"}, CLI)
    assert res.proposal and not res.executed and res.proposal.summary == "Pause queue default"
    assert not (await ops.kit.jobs.stats())[0].paused  # nothing happened
    done = await ops.client.call("queue_pause", {"queue": "default"}, CLI, confirm=True)
    assert done.executed and (await ops.kit.jobs.stats())[0].paused


async def test_bad_input_says_where_not_what(ops: Ops) -> None:
    with pytest.raises(InvalidInput, match=r"queue_pause: queue: Field required"):
        await ops.client.call("queue_pause", {}, CLI)


async def test_an_mcp_caller_can_only_confirm_with_the_token_its_own_proposal_produced(ops: Ops) -> None:
    await ops.client.exposure.set("op:queue_pause", True, actor=USER)  # the owner turns it on
    agent = await mcp_agent(ops, "queue_pause")
    first = await ops.client.call("queue_pause", {"queue": "default"}, agent, confirm=True)  # confirm is ignored
    assert first.proposal is not None and not (await ops.kit.jobs.stats())[0].paused
    other = await mcp_agent(ops, "queue_pause")
    with pytest.raises(Exception, match="different call"):  # a token is bound to the caller
        await ops.client.call("queue_pause", {"queue": "default"}, other, confirmation_token=first.proposal.confirmation_token)
    with pytest.raises(Exception, match="unknown, used already"):  # and a wrong attempt spends it
        await ops.client.call("queue_pause", {"queue": "default"}, agent, confirmation_token=first.proposal.confirmation_token)
    again = (await ops.client.call("queue_pause", {"queue": "default"}, agent)).proposal
    assert again is not None
    done = await ops.client.call("queue_pause", {"queue": "default"}, agent, confirmation_token=again.confirmation_token)
    assert done.executed and (await ops.kit.jobs.stats())[0].paused


async def test_off_means_the_same_words_as_never_and_never_cannot_be_turned_on(ops: Ops) -> None:
    agent = await mcp_agent(ops, "queue_pause")
    with pytest.raises(NotExposed):  # queue_pause is off by default
        await ops.client.call("queue_pause", {"queue": "default"}, agent)
    with pytest.raises(InvalidInput, match="never offered over MCP"):
        await ops.client.call("exposure_set", {"name": "queue_requeue", "enabled": True}, CLI, confirm=True)
    with pytest.raises(NotExposed):
        await ops.client.call("exposure_set", {"name": "status", "enabled": False}, agent)
    with pytest.raises(InvalidInput, match="never offered over MCP"):
        await ops.agents.create("x", ["work"])  # an agent can be made, but a grant on a never op is refused:
        await ops.client.call("agent_grant", {"agent_id": agent.agent_id, "operations": ["queue_requeue"]}, CLI, confirm=True)


async def test_the_config_file_can_lift_one_never(ops: Ops) -> None:
    ops.client.exposure.lifted = frozenset({"queue_requeue"})
    await ops.client.call("exposure_set", {"name": "queue_requeue", "enabled": True}, CLI, confirm=True)
    agent = await mcp_agent(ops, "queue_requeue")
    res = await ops.client.call("queue_requeue", {"status": "failed"}, agent)
    assert res.proposal is not None  # lifted to a gated, previewed, owner-enabled, agent-granted call


async def test_an_agent_without_a_grant_gets_the_job_protocol_and_nothing_else(ops: Ops) -> None:
    bare = await mcp_agent(ops)
    assert {o.name for o in await ops.client.visible(bare)} == {"job_claim", "job_heartbeat", "job_complete", "job_fail"}
    with pytest.raises(NotExposed):
        await ops.client.call("label_get", {"subject_id": "s"}, bare)
    granted = await mcp_agent(ops, "label_get")
    assert (await ops.client.call("label_get", {"subject_id": "s"}, granted)).executed


async def test_the_job_protocol_end_to_end_for_an_agent(ops: Ops) -> None:
    q = ops.kit.jobs
    q.register_result("work", Pong)
    await q.enqueue("work", Ping(n=4), subject="s1")
    agent = await mcp_agent(ops)
    claim = (await ops.client.call("job_claim", {}, agent)).output
    assert claim.job and claim.job.payload == {"n": 4} and "instruction to you" in claim.job.note  # type: ignore[union-attr]
    other = await mcp_agent(ops)
    with pytest.raises(Conflict, match="lease_lost"):
        await ops.client.call("job_complete", {"job_id": claim.job.id, "result": {"n": 5}}, other)  # type: ignore[union-attr]
    hb = (await ops.client.call("job_heartbeat", {"job_id": claim.job.id}, agent)).output  # type: ignore[union-attr]
    assert hb.cancel_requested is False  # type: ignore[union-attr]
    await ops.client.call("job_complete", {"job_id": claim.job.id, "result": {"n": 5}, "produced_by": "claude"}, agent)  # type: ignore[union-attr]
    done = await q.get(claim.job.id)  # type: ignore[union-attr, arg-type]
    assert done and (done.status, done.produced_by) == ("succeeded", "claude")
    assert (await ops.client.call("job_claim", {}, agent)).output.job is None  # type: ignore[union-attr]


async def test_the_job_protocol_needs_an_agent_token_and_a_revoked_one_stops_at_once(ops: Ops) -> None:
    with pytest.raises(PermissionDenied, match="agent token"):
        await ops.client.call("job_claim", {}, CLI)
    agent = await mcp_agent(ops)
    await ops.client.call("agent_revoke", {"agent_id": agent.agent_id}, CLI, confirm=True)
    with pytest.raises(PermissionDenied, match="revoked"):
        await ops.client.call("job_claim", {}, agent)
    with pytest.raises(PermissionDenied):
        await ops.client.visible(agent)


async def test_a_person_labels_the_human_layer_an_agent_the_machine_layer(ops: Ops) -> None:
    await ops.client.call("label_set", {"subject_id": "s1", "key": "priority", "value": "urgent"}, CLI, confirm=True)
    await ops.client.exposure.set("op:label_set", True, actor=USER)
    agent = await mcp_agent(ops, "label_set")
    p = (await ops.client.call("label_set", {"subject_id": "s1", "key": "priority", "value": "low"}, agent)).proposal
    assert p is not None
    await ops.client.call("label_set", {"subject_id": "s1", "key": "priority", "value": "low"}, agent, confirmation_token=p.confirmation_token)
    got = (await ops.client.call("label_get", {"subject_id": "s1", "key": "priority"}, CLI)).output.labels[0]  # type: ignore[union-attr]
    assert (got.effective, got.human, got.machine) == ("urgent", "urgent", "low")  # the agent could not overwrite
    with pytest.raises(NotExposed):
        await ops.client.call("label_clear", {"subject_id": "s1", "key": "priority"}, agent)  # human-only
    await ops.client.call("label_clear", {"subject_id": "s1", "key": "priority"}, CLI, confirm=True)
    assert (await ops.kit.labels.get("s1", "priority")) == "low"


async def test_a_bad_label_fails_in_the_preview_before_anything_is_proposed(ops: Ops) -> None:
    from oaken_core.labels import LabelError

    with pytest.raises(LabelError):
        await ops.client.call("label_set", {"subject_id": "s", "key": "priority", "value": "soon"}, CLI)


async def test_requeue_previews_the_jobs_then_runs_for_a_person_and_never_for_mcp(ops: Ops) -> None:
    q = ops.kit.jobs
    await q.enqueue("k", Ping(), max_attempts=1)
    job = await q.claim("w", "default", ["k"])
    assert job and await q.fail(job.id, "w", "boom") == "failed"
    res = await ops.client.call("queue_requeue", {"status": "failed"}, CLI)
    assert res.proposal and res.proposal.total_changes == 1 and res.proposal.changes[0].target == job.id
    await ops.client.call("queue_requeue", {"status": "failed"}, CLI, confirm=True)
    assert (await q.get(job.id)).status == "queued"  # type: ignore[union-attr]
    with pytest.raises(NotExposed):
        await ops.client.call("queue_requeue", {"status": "failed"}, await mcp_agent(ops, "queue_requeue"))


async def test_agent_create_shows_the_token_once_and_the_list_never_does(ops: Ops) -> None:
    out = (await ops.client.call("agent_create", {"label": "librarian", "kinds": ["work"]}, CLI, confirm=True)).output
    assert out.token.startswith("oaken_agent_")  # type: ignore[union-attr]
    listed = (await ops.client.call("agent_list", {}, CLI)).output
    assert out.token not in json.dumps(listed.model_dump())  # type: ignore[union-attr]
    with pytest.raises(InvalidInput):
        await ops.client.call("agent_create", {"label": "x", "kinds": ["work"], "operations": ["no_such_op"]}, CLI, confirm=True)  # type: ignore[call-arg]


async def test_status_and_the_log_and_the_audit_row_holds_a_hash_not_the_input(ops: Ops) -> None:
    st = (await ops.client.call("status", {}, CLI)).output
    assert st.version == "9.9.9" and st.queues[0].queue == "default"  # type: ignore[union-attr]
    await ops.client.call("queue_pause", {"queue": "default"}, CLI, confirm=True)
    rows = (await ops.client.call("log_list", {"key": "call:queue_pause"}, CLI)).output.rows  # type: ignore[union-attr]
    meta = json.loads(rows[0].new_value or "")
    assert meta["client"] == "cli" and len(meta["input_sha256"]) == 64 and "default" not in rows[0].new_value  # type: ignore[operator]


def test_cli_paths_and_group_rules(ops: Ops) -> None:
    assert ops.registry.get("queue_requeue").cli_path == ("queue", "requeue")
    assert ops.registry.get("status").cli_path == ("status",)

    class E(BaseModel):
        pass

    async def h(ctx: object, inp: E) -> E:
        return E()

    with pytest.raises(OperationDefinitionError, match="must prefix"):
        Operation(name="thing", summary="x", permission="read", input=E, output=E, handler=h, group="queue")


async def test_exposure_list_names_each_operations_provider(ops: Ops) -> None:
    rows = {o.name: o for o in (await ops.client.call("exposure_list", {}, CLI)).output.operations}  # type: ignore[union-attr]
    assert rows["cache_clear"].provider == "core.provider.cache" and rows["status"].provider == "core.provider.general"
