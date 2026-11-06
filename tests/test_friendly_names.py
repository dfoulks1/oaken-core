# SPDX-License-Identifier: Apache-2.0
"""Friendly ``*.localhost`` host names (BaseFriendlyNames): the guards accept them, and only them."""

import httpx2
import pytest

from oaken_core.api.guard import Policy
from oaken_core.api.ratelimit import RateLimiter
from oaken_core.console.guard import ConsoleGuard, PortRef
from oaken_core.plugins.host import PluginHost

PORT = 4321


async def ok(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def client(names: frozenset[str]) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=ConsoleGuard(ok, PortRef(PORT), lambda: names)), base_url="http://x")


@pytest.mark.parametrize("host,status", [
    (f"oaken.localhost:{PORT}", 200), (f"OAKEN.localhost:{PORT}", 200), ("oaken.localhost:9", 400),
    (f"evil.example:{PORT}", 400), (f"oaken.example:{PORT}", 400), (f"127.0.0.1:{PORT}", 200),
])  # fmt: skip
async def test_the_console_accepts_a_plugin_name_with_its_port_and_nothing_else(host: str, status: int) -> None:
    async with client(frozenset({"oaken.localhost", "oaken.example", "a:b.localhost"})) as c:
        assert (await c.get("/", headers={"host": host})).status_code == status


async def test_the_console_refuses_a_plugin_name_that_is_not_localhost_even_if_a_plugin_asks() -> None:
    async with client(frozenset({"oaken.example"})) as c:
        assert (await c.get("/", headers={"host": f"oaken.example:{PORT}"})).status_code == 400


async def test_a_friendly_origin_may_post_to_the_console() -> None:
    async with client(frozenset({"oaken.localhost"})) as c:
        post = lambda o: c.post("/", data={"a": "1"}, headers={"host": f"oaken.localhost:{PORT}", "origin": o})  # noqa: E731
        assert (await post(f"http://oaken.localhost:{PORT}")).status_code == 200
        assert (await post("http://evil.example")).status_code == 403


def test_the_api_policy_adds_only_localhost_names_with_the_port() -> None:
    p = Policy(limiter=RateLimiter(60), public_csp="")
    p.bind(PORT)
    p.names = lambda: frozenset({"oakenmcp.localhost", "oaken.example", "x:1.localhost"})
    assert p.friendly() == {f"oakenmcp.localhost:{PORT}"}


def test_the_host_collects_names_from_plugins_that_have_the_hooks() -> None:
    class Rec:
        started = healthy = True
        kind, name = "gateway", "n"

        class instance:  # noqa: N801
            console_names = staticmethod(lambda: ["Oaken.localhost"])

    host = PluginHost.__new__(PluginHost)
    host._records = {"n": Rec()}  # type: ignore[attr-defined]
    assert host.console_names() == {"oaken.localhost"} and host.api_names() == frozenset()
