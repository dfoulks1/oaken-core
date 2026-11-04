# SPDX-License-Identifier: Apache-2.0
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest

from oaken_core.actors import USER
from oaken_core.api import Api
from oaken_core.api.mcp_server import McpServer
from oaken_core.events import EventBus, JobStateChanged
from oaken_core.ids import JobId

from .conftest import Ops
from .test_jobs import Ping, Pong

HOST = "http://127.0.0.1:8123"
TOKEN = "general-token"
JSON = {"content-type": "application/json"}


@pytest.fixture
async def web(ops: Ops) -> AsyncIterator[tuple[Api, httpx2.AsyncClient]]:
    api = Api(ops.client, token=TOKEN)
    api.bind(8123)
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=api), base_url=HOST,
                                headers={"authorization": f"Bearer {TOKEN}"})  # fmt: skip
    try:
        yield api, client
    finally:
        await client.aclose()


async def test_health_is_public_and_everything_else_needs_the_token(web: tuple[Api, httpx2.AsyncClient]) -> None:
    _, c = web
    assert (await c.get("/v1/health", headers={"authorization": ""})).json() == {"status": "ok"}
    assert (await c.get("/v1/operations", headers={"authorization": ""})).status_code == 401
    assert (await c.get("/v1/operations", headers={"authorization": "Bearer nope"})).status_code == 401


async def test_the_guard_refuses_a_foreign_host_a_cross_origin_post_and_a_non_json_body(web: tuple[Api, httpx2.AsyncClient]) -> None:
    _, c = web
    assert (await c.get("/v1/health", headers={"host": "evil.example"})).status_code == 400
    r = await c.post("/v1/operations/status", json={}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    assert (await c.post("/v1/operations/status", content=b"x", headers={"content-type": "text/plain"})).status_code == 415


async def test_a_person_sees_every_operation_and_the_mcp_class_only_what_is_on(web: tuple[Api, httpx2.AsyncClient]) -> None:
    _, c = web
    every = {o["name"] for o in (await c.get("/v1/operations")).json()["operations"]}
    mcp = {o["name"] for o in (await c.get("/v1/operations", headers={"x-oaken-client": "mcp"})).json()["operations"]}
    assert "agent_create" in every and "agent_create" not in mcp and "status" in mcp
    assert (await c.get("/v1/operations", headers={"x-oaken-client": "root"})).status_code == 400


async def test_calling_an_operation_proposes_then_runs_and_errors_have_codes(web: tuple[Api, httpx2.AsyncClient], ops: Ops) -> None:
    _, c = web
    r = (await c.post("/v1/operations/queue_pause", json={"input": {"queue": "default"}})).json()
    assert r["status"] == "confirmation_required" and r["proposal"]["summary"] == "Pause queue default"
    assert not (await ops.kit.queue.stats())[0].paused
    done = (await c.post("/v1/operations/queue_pause", json={"input": {"queue": "default"}, "confirm": True})).json()
    assert done["status"] == "ok" and (await ops.kit.queue.stats())[0].paused
    missing = await c.post("/v1/operations/no_such_op", json={})
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"
    bad = await c.post("/v1/operations/queue_pause", json={"input": {}})
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_input"
    extra = await c.post("/v1/operations/status", json={"surprise": 1})
    assert extra.status_code == 422  # unknown body fields are refused


async def test_the_openapi_document_lists_the_operations(web: tuple[Api, httpx2.AsyncClient]) -> None:
    _, c = web
    doc = (await c.get("/v1/openapi.json")).json()
    assert doc["info"]["title"] == "Oaken Core" and any("queue_requeue" in p for p in doc["paths"])


async def test_an_agent_token_opens_the_job_routes_and_nothing_else(web: tuple[Api, httpx2.AsyncClient], ops: Ops) -> None:
    _, c = web
    q = ops.kit.queue
    q.register_result("work", Pong)
    await q.enqueue("work", Ping(n=1), subject="s1")
    agent, token = await ops.agents.create("librarian", ["work"])
    a = {"authorization": f"Bearer {token}"}
    assert (await c.get("/v1/operations", headers=a)).status_code == 401  # an agent token opens only /v1/agent/
    assert (await c.post("/v1/agent/jobs/claim", json={}, headers={"authorization": f"Bearer {TOKEN}"})).status_code == 401
    claim = (await c.post("/v1/agent/jobs/claim", json={}, headers=a)).json()
    job = claim["job"]
    assert job["kind"] == "work" and job["payload"] == {"n": 1}
    assert (await c.post(f"/v1/agent/jobs/{job['id']}/heartbeat", json={}, headers=a)).json() == {"cancel_requested": False}
    done = await c.post(f"/v1/agent/jobs/{job['id']}/complete", json={"result": {"n": 2}, "produced_by": "claude"}, headers=a)
    assert done.json() == {"ok": True}
    stored = await q.get(JobId(job["id"]))
    assert stored and (stored.status, stored.agent_id) == ("succeeded", agent.id)
    empty = (await c.post("/v1/agent/jobs/claim", json={}, headers=a)).json()
    assert empty["job"] is None
    gone = await c.post(f"/v1/agent/jobs/{job['id']}/complete", json={"result": {"n": 3}}, headers=a)
    assert gone.status_code == 409  # the lease is gone


async def test_a_revoked_agent_token_stops_working(web: tuple[Api, httpx2.AsyncClient], ops: Ops) -> None:
    _, c = web
    agent, token = await ops.agents.create("a", ["work"])
    await ops.agents.revoke(agent.id)
    assert (await c.post("/v1/agent/jobs/claim", json={}, headers={"authorization": f"Bearer {token}"})).status_code == 401


async def test_the_event_ring_numbers_what_the_bus_publishes(web: tuple[Api, httpx2.AsyncClient], ops: Ops) -> None:
    api, _ = web
    ops.svc.bus = EventBus()
    api.start()
    await ops.svc.bus.publish(JobStateChanged(ts=ops.kit.clock.now(), job_id=JobId("j"), kind="k", queue="default", status="queued"))
    assert api.ring.last_id == 1
    frames, gap = api.ring.after(0)
    assert [f.name for f in frames] == ["JobStateChanged"] and not gap
    api.stop()


# -- MCP: the agent door is the same token, same operations ---------------------------------
async def rpc(c: httpx2.AsyncClient, path: str, method: str, params: dict[str, Any], token: str) -> httpx2.Response:
    headers = {"authorization": f"Bearer {token}", "accept": "application/json, text/event-stream", **JSON}
    return await c.post(path, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, headers=headers)


async def tool_text(c: httpx2.AsyncClient, path: str, token: str, name: str, args: dict[str, Any]) -> tuple[str, bool]:
    res = (await rpc(c, path, "tools/call", {"name": name, "arguments": args}, token)).json()["result"]
    return res["content"][0]["text"], bool(res.get("isError"))


@pytest.fixture
async def mcp_web(ops: Ops, web: tuple[Api, httpx2.AsyncClient]) -> AsyncIterator[tuple[httpx2.AsyncClient, McpServer]]:
    api, c = web
    person, agent_door = McpServer(ops.client, token="person-mcp"), McpServer(ops.client, agents=ops.agents)
    api.mount(person.surface, person)
    api.mount(agent_door.surface, agent_door)
    await person.start()
    await agent_door.start()
    try:
        yield c, agent_door
    finally:
        await person.stop()
        await agent_door.stop()


async def test_each_mcp_door_takes_only_its_own_token(mcp_web: tuple[httpx2.AsyncClient, McpServer], ops: Ops) -> None:
    c, _ = mcp_web
    _, agent_token = await ops.agents.create("claude", ["work"])
    assert (await rpc(c, "/mcp", "tools/list", {}, agent_token)).status_code == 401
    assert (await rpc(c, "/mcp/agent", "tools/list", {}, "person-mcp")).status_code == 401
    assert (await rpc(c, "/mcp/agent", "tools/list", {}, TOKEN)).status_code == 401


async def test_the_agent_door_lists_the_job_tools_plus_only_what_was_granted(mcp_web: tuple[httpx2.AsyncClient, McpServer], ops: Ops) -> None:
    c, _ = mcp_web
    _, bare = await ops.agents.create("bare", ["work"])
    _, granted = await ops.agents.create("granted", ["work"], operations=["label_get"])

    async def names(token: str) -> set[str]:
        tools = (await rpc(c, "/mcp/agent", "tools/list", {}, token)).json()["result"]["tools"]
        return {t["name"] for t in tools}

    assert await names(bare) == {"job_claim", "job_heartbeat", "job_complete", "job_fail"}
    assert await names(granted) == {"job_claim", "job_heartbeat", "job_complete", "job_fail", "label_get"}


async def test_an_agent_works_a_job_over_mcp_and_the_payload_arrives_fenced_as_data(mcp_web: tuple[httpx2.AsyncClient, McpServer], ops: Ops) -> None:
    c, _ = mcp_web
    q = ops.kit.queue
    q.register_result("work", Pong)
    await q.enqueue("work", Ping(n=7), subject="s1")
    _, token = await ops.agents.create("claude", ["work"])
    text, err = await tool_text(c, "/mcp/agent", token, "job_claim", {})
    assert not err and "<<<DATA" in text and "untrusted data" in text and '"n":7' in text
    job_id = json.loads(next(ln for ln in text.split("\n") if ln.startswith("{")))["output"]["job"]["id"]
    done, err = await tool_text(c, "/mcp/agent", token, "job_complete", {"job_id": job_id, "result": {"n": 8}})
    assert not err and '"ok":true' in done


async def test_over_mcp_an_ungranted_or_never_tool_is_the_same_error_and_a_granted_mutation_proposes(mcp_web: tuple[httpx2.AsyncClient, McpServer], ops: Ops) -> None:
    c, _ = mcp_web
    await ops.client.exposure.set("op:queue_pause", True, actor=USER)
    _, bare = await ops.agents.create("bare", ["work"])
    _, can = await ops.agents.create("can", ["work"], operations=["queue_pause"])
    for token, tool in ((bare, "queue_pause"), (bare, "agent_create"), (can, "queue_requeue")):
        text, err = await tool_text(c, "/mcp/agent", token, tool, {"queue": "default"})
        assert err and "not available to MCP clients" in text
    text, err = await tool_text(c, "/mcp/agent", can, "queue_pause", {"queue": "default"})
    body = json.loads(text)
    assert not err and body["status"] == "confirmation_required" and body["proposal"]["confirmation_token"]
    assert not (await ops.kit.queue.stats())[0].paused  # the first call changed nothing
