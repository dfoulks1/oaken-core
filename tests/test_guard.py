# SPDX-License-Identifier: Apache-2.0
"""The browser-safety and authentication rules, one attacker at a time."""

import hmac
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import pytest

from oaken_core.api import Api
from oaken_core.api import runtime as runtime_module
from oaken_core.api.server import ApiServer, check_bind
from oaken_core.errors import ConfigError
from oaken_core.settings import ApiSettings

from .conftest import Ops

PORT = 8123
ORIGIN = f"http://127.0.0.1:{PORT}"
TOKEN = "guard-token"


@dataclass
class Rig:
    api: Api
    http: httpx2.AsyncClient

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {TOKEN}"}

    async def get(self, path: str, headers: dict[str, str] | None = None) -> httpx2.Response:
        return await self.http.get(path, headers={**self.auth, **(headers or {})})

    async def post(self, path: str, body: Any = None, headers: dict[str, str] | None = None) -> httpx2.Response:
        return await self.http.post(path, json={"input": {}} if body is None else body,
                                    headers={**self.auth, **(headers or {})})  # fmt: skip


@pytest.fixture
def make_rig(ops: Ops) -> Callable[..., Any]:
    @asynccontextmanager
    async def build(**api: Any) -> AsyncIterator[Rig]:
        ops.svc.settings.api = ApiSettings(**api)
        app = Api(ops.client, token=TOKEN)
        app.bind(PORT)
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=ORIGIN)
        try:
            yield Rig(app, http)
        finally:
            await http.aclose()

    return build


@pytest.fixture
async def rig(make_rig: Callable[..., Any]) -> AsyncIterator[Rig]:
    async with make_rig() as r:
        yield r


async def test_health_is_the_one_open_route_and_says_only_that_it_is_up(rig: Rig) -> None:
    r = await rig.http.get("/v1/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/v1/operations", "/v1/openapi.json", "/v1/events"])
async def test_every_other_route_needs_the_token(rig: Rig, path: str) -> None:
    assert (await rig.http.get(path)).status_code == 401
    wrong = await rig.http.get(path, headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    assert (await rig.http.get(path, headers={"Authorization": TOKEN})).status_code == 401  # no scheme
    assert wrong.json()["code"] == "unauthorized"


async def test_the_token_is_compared_in_constant_time(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr("oaken_core.api.runtime.hmac.compare_digest", spy)
    await rig.http.get("/v1/operations", headers={"Authorization": "Bearer guess"})
    assert seen, "the token must go through hmac.compare_digest"
    assert all(len(a) == len(b) == 32 for a, b in seen), "digests, so length leaks nothing"


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1:1", "localhost", "127.0.0.1"])
async def test_a_wrong_host_is_refused_before_anything_else(rig: Rig, host: str) -> None:
    r = await rig.http.get("/v1/health", headers={"Host": host})
    assert r.status_code == 400  # DNS rebinding: the page's origin is theirs, the Host shows it


@pytest.mark.parametrize("host", [f"127.0.0.1:{PORT}", f"localhost:{PORT}"])
async def test_the_two_loopback_names_with_the_port_are_allowed(rig: Rig, host: str) -> None:
    assert (await rig.http.get("/v1/health", headers={"Host": host})).status_code == 200


async def test_a_cross_origin_post_is_refused_even_with_the_token(rig: Rig) -> None:
    assert (await rig.post("/v1/operations/status", headers={"Origin": "http://evil.example"})).status_code == 403
    assert (await rig.post("/v1/operations/status", headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
    assert (await rig.post("/v1/operations/status", headers={"Origin": ORIGIN})).status_code == 200


async def test_no_cors_headers_unless_a_dev_origin_is_configured(rig: Rig) -> None:
    r = await rig.get("/v1/operations", headers={"Origin": "http://localhost:5173"})
    assert not any(k.lower().startswith("access-control-") for k in r.headers)
    pre = await rig.http.options(
        "/v1/operations/status",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
    )
    assert pre.status_code != 204 and "access-control-allow-origin" not in pre.headers


async def test_the_configured_dev_origin_gets_cors_and_nobody_else(make_rig: Callable[..., Any]) -> None:
    async with make_rig(dev_origin="http://localhost:5173") as rig:
        pre = await rig.http.options(
            "/v1/operations/status",
            headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
        )
        assert pre.status_code == 204
        assert pre.headers["access-control-allow-origin"] == "http://localhost:5173"
        other = await rig.http.options(
            "/v1/operations/status",
            headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
        )
        assert "access-control-allow-origin" not in other.headers
        assert (await rig.post("/v1/operations/status", headers={"Origin": "http://localhost:5173"})).status_code == 200
        assert (await rig.post("/v1/operations/status", headers={"Origin": "http://evil.example"})).status_code == 403


async def test_a_post_must_be_json(rig: Rig) -> None:
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
        r = await rig.http.post("/v1/operations/status", content=b"{}", headers={**rig.auth, "Content-Type": ctype})
        assert r.status_code == 415, ctype  # the "simple" types a form can send cross-origin


async def test_a_body_over_the_limit_is_refused(make_rig: Callable[..., Any]) -> None:
    async with make_rig(max_body_bytes=2048) as rig:
        assert (await rig.post("/v1/operations/status", {"input": {"q": "x" * 5000}})).status_code == 413
        assert (await rig.post("/v1/operations/status", {"input": {}})).status_code == 200


async def test_mutating_requests_are_rate_limited_per_client(make_rig: Callable[..., Any]) -> None:
    async with make_rig(rate_limit_per_minute=10) as rig:
        codes = [(await rig.post("/v1/operations/status")).status_code for _ in range(40)]
        assert 200 in codes and 429 in codes
        limited = await rig.post("/v1/operations/status")
        assert limited.status_code == 429 and "retry-after" in limited.headers
        assert (await rig.get("/v1/operations")).status_code == 200, "reads are not limited"


async def test_a_wrong_token_never_reaches_the_handler(rig: Rig) -> None:
    r = await rig.http.post("/v1/operations/status", json={}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_responses_carry_the_hardening_headers(rig: Rig) -> None:
    r = await rig.get("/v1/operations")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "server" not in r.headers


async def test_a_page_outside_the_api_is_public_and_has_a_strict_csp(rig: Rig) -> None:
    r = await rig.http.get("/")
    assert r.status_code != 401  # no token needed: nothing here but the CSP
    assert "default-src" in r.headers["content-security-policy"]


# -- binding ---------------------------------------------------------------------------------
def test_a_non_loopback_bind_needs_remote_ok() -> None:
    for host in ("0.0.0.0", "::", "192.168.1.5", "example.com"):  # noqa: S104
        with pytest.raises(ConfigError):
            check_bind(ApiSettings(host=host))
        check_bind(ApiSettings(host=host, remote_ok=True))  # said on purpose
    for host in ("127.0.0.1", "::1", "localhost"):
        check_bind(ApiSettings(host=host))


def test_the_server_itself_refuses_a_non_loopback_host(ops: Ops) -> None:
    ops.svc.settings.api = ApiSettings(host="0.0.0.0")  # noqa: S104
    with pytest.raises(ConfigError):
        ApiServer(Api(ops.client, token=TOKEN))


# -- files -----------------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_token_file_is_private_and_a_loose_one_is_tightened(tmp_path: Path) -> None:
    path = tmp_path / "api_token"
    token = runtime_module.load_or_create_token(path)
    assert len(token) >= 40 and path.stat().st_mode & 0o777 == 0o600
    assert runtime_module.load_or_create_token(path) == token, "stable across starts"
    path.chmod(0o644)
    assert runtime_module.load_or_create_token(path) == token
    assert path.stat().st_mode & 0o777 == 0o600


def test_the_console_port_defaults_to_oaken_and_config_toml_overrides_it(tmp_path: Path) -> None:
    from oaken_core.settings import ConsoleSettings, Settings

    assert ConsoleSettings().port == 62536  # OAKEN on a phone keypad
    assert ApiSettings().port == 0  # the API picks a free one and publishes it
    cfg = tmp_path / "config.toml"
    assert Settings.load(cfg).console.port == 62536  # no file: the default
    cfg.write_text("[console]\nport = 7777\n")
    assert Settings.load(cfg).console.port == 7777
