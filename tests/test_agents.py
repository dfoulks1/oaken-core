# SPDX-License-Identifier: Apache-2.0
import pytest
from pydantic import BaseModel

from oaken_core.agents import AgentRegistry
from oaken_core.errors import ConfigError, PermissionDenied, QuotaExceeded
from oaken_core.events import EventBus, JobStateChanged
from oaken_core.jobs import Jobs
from oaken_core.settings import JobsSettings, SwarmSettings

from .conftest import Kit
from .test_jobs import Ping, Pong


class Private(BaseModel):
    labels: dict[str, str] = {}
    confidential: bool = False


@pytest.fixture
def swarm() -> SwarmSettings:
    return SwarmSettings(enabled=True, allowed_kinds=["llm.summarize"], allowed_queues=["llm"], default_max_leases=1,
                         default_max_per_hour=2, agent_lease_seconds=300)  # fmt: skip


@pytest.fixture
def agents(kit: Kit, swarm: SwarmSettings) -> AgentRegistry:
    kit.jobs.swarm = swarm
    return AgentRegistry(kit.db, kit.clock, swarm)


async def test_a_token_is_shown_once_and_only_its_hash_is_stored(kit: Kit, agents: AgentRegistry) -> None:
    agent, token = await agents.create("librarian", ["llm.summarize"])
    assert token.startswith("oaken_agent_")
    stored = await kit.db.fetchone("SELECT token_hash FROM agents WHERE id = ?", (agent.id,))
    assert stored and token not in stored["token_hash"]
    assert (await agents.authenticate(token)).id == agent.id
    with pytest.raises(PermissionDenied):
        await agents.authenticate("oaken_agent_wrong")


async def test_the_swarm_is_off_by_default_and_kinds_are_opt_in(kit: Kit, swarm: SwarmSettings) -> None:
    with pytest.raises(PermissionDenied, match="disabled"):
        await AgentRegistry(kit.db, kit.clock, SwarmSettings()).create("a", ["llm.summarize"])
    reg = AgentRegistry(kit.db, kit.clock, swarm)
    with pytest.raises(ConfigError, match="not allowed"):
        await reg.create("a", ["intake.triage"])  # never offered to an agent unless opted in


async def test_an_agent_claims_inside_its_scope_and_the_lease_is_longer(kit: Kit, agents: AgentRegistry) -> None:
    kit.queues.new_queue("llm")
    await kit.jobs.enqueue("llm.summarize", Ping(), queue="llm")
    await kit.jobs.enqueue("other", Ping(), queue="llm")
    scoped, _ = await agents.create("only-default", ["llm.summarize"], queues=["default"])
    assert await kit.jobs.claim("x", "llm", ["llm.summarize", "other"], agent=scoped) is None  # wrong queue
    wide, _ = await agents.create("wide", ["llm.summarize"])
    job = await kit.jobs.claim("x", "llm", ["llm.summarize", "other"], agent=wide)
    assert job and job.kind == "llm.summarize" and job.locked_by == wide.worker  # "other" was out of scope
    assert (job.locked_until - kit.clock.now()).total_seconds() == 300  # type: ignore[operator]


async def test_a_queue_scoped_agent_gets_only_kinds_core_can_adopt_and_nothing_confidential(kit: Kit, agents: AgentRegistry) -> None:
    kit.queues.new_queue("llm")
    kit.jobs.adoptable.add("llm.summarize")  # what JobRunner.register(adopt=) records
    with pytest.raises(ConfigError, match="allowed_queues"):
        await agents.create("q", queues=["default"])  # with no kinds the queue is the scope, and it is opted in by name
    agent, _ = await agents.create("q", queues=["llm"])
    await kit.jobs.enqueue("other", Ping(), queue="llm")
    assert await kit.jobs.claim("x", "llm", ["other"], agent=agent) is None  # naming a kind does not widen the scope
    await kit.jobs.enqueue("llm.summarize", Private(labels={"confidential": "true"}), queue="llm")
    await kit.jobs.enqueue("llm.summarize", Private(confidential=True), queue="llm")
    assert await kit.jobs.claim("x", "llm", ["llm.summarize"], agent=agent) is None  # both are private, however the payload says so
    await kit.jobs.enqueue("llm.summarize", Private(labels={"confidential": "false"}), queue="llm")
    job = await kit.jobs.claim("x", "llm", ["llm.summarize"], agent=agent)
    assert job and job.kind == "llm.summarize" and '"false"' in job.payload_json


async def test_quotas_hold_inside_the_claim(kit: Kit, agents: AgentRegistry) -> None:
    for _ in range(3):
        await kit.jobs.enqueue("llm.summarize", Ping())
    agent, _ = await agents.create("a", ["llm.summarize"])
    first = await kit.jobs.claim("x", "default", ["llm.summarize"], agent=agent)
    assert first
    with pytest.raises(QuotaExceeded, match="holds 1"):
        await kit.jobs.claim("x", "default", ["llm.summarize"], agent=agent)
    await kit.jobs.complete(first.id, agent.worker)  # frees the lease, but the hour counts it
    kit.jobs.result_models.pop("llm.summarize", None)
    second = await kit.jobs.claim("x", "default", ["llm.summarize"], agent=agent)
    assert second
    await kit.jobs.complete(second.id, agent.worker)
    with pytest.raises(QuotaExceeded, match="last hour"):
        await kit.jobs.claim("x", "default", ["llm.summarize"], agent=agent)
    kit.clock.advance(hours=1, seconds=1)
    assert await kit.jobs.claim("x", "default", ["llm.summarize"], agent=agent) is not None


async def test_an_agent_finishes_a_job_and_says_which_model_made_it(kit: Kit, agents: AgentRegistry) -> None:
    kit.jobs.register_result("llm.summarize", Pong)
    await kit.jobs.enqueue("llm.summarize", Ping())
    agent, _ = await agents.create("a", ["llm.summarize"])
    job = await kit.jobs.claim("x", "default", ["llm.summarize"], agent=agent)
    assert job and await kit.jobs.complete(job.id, agent.worker, Pong(n=1), produced_by="claude")
    done = await kit.jobs.get(job.id)
    assert done and (done.status, done.produced_by, done.agent_id) == ("succeeded", "claude", agent.id)


async def test_revoking_hands_the_job_back_without_counting_the_attempt(kit: Kit, agents: AgentRegistry) -> None:
    await kit.jobs.enqueue("llm.summarize", Ping())
    agent, token = await agents.create("a", ["llm.summarize"])
    job = await kit.jobs.claim("x", "default", ["llm.summarize"], agent=agent)
    assert job
    assert await agents.revoke(agent.id) is True and await agents.revoke(agent.id) is False
    back = await kit.jobs.get(job.id)
    assert back and (back.status, back.attempts) == ("queued", 0)
    with pytest.raises(PermissionDenied):
        await agents.authenticate(token)
    with pytest.raises(PermissionDenied):
        await kit.jobs.claim("x", "default", ["llm.summarize"], agent=agent)
    assert await kit.jobs.heartbeat(job.id, agent.worker) == (False, False)  # no longer holds it


async def test_job_state_changes_reach_the_event_bus(kit: Kit) -> None:
    bus = EventBus()
    jobs = Jobs(kit.db, kit.clock, kit.queues, labels=kit.labels, changelog=kit.log,
                     settings=JobsSettings(backoff_jitter=0), bus=bus)  # fmt: skip
    stream = bus.stream(JobStateChanged)
    enq = await jobs.enqueue("k", Ping(), subject="s1")
    job = await jobs.claim("w", "default", ["k"])
    assert job and await jobs.complete(job.id, "w") is True
    seen = [(await stream.__anext__()).status for _ in range(3)]  # type: ignore[attr-defined]
    assert seen == ["queued", "running", "succeeded"] and enq.created
    await stream.aclose()
