# SPDX-License-Identifier: Apache-2.0
"""The browser-safety and authentication rules, one attacker at a time."""

import hmac
import sys
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from oaken_core.api import ApiServer
from oaken_core.api import runtime as runtime_module
from oaken_core.api.tests.conftest import ORIGIN, PORT, TOKEN, Rig
from oaken_core.errors import ConfigError
from oaken_core.settings import APISettings


async def test_health_is_the_one_open_route_and_says_only_that_it_is_up(rig: Rig) -> None:
    r = await rig.http.get("/v1/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/v1/operations", "/v1/openapi.json", "/v1/events"])
async def test_every_other_route_needs_the_token(rig: Rig, path: str) -> None:
    assert (await rig.http.get(path)).status_code == 401
    wrong = await rig.http.get(path, headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    assert (await rig.http.get(path, headers={"Authorization": TOKEN})).status_code == 401
    assert wrong.json()["code"] == "unauthorized"


async def test_the_token_is_compared_in_constant_time(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    r = await rig.post("/v1/operations/status", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = await rig.post("/v1/operations/status", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    same = await rig.post("/v1/operations/status", headers={"Origin": ORIGIN})
    assert same.status_code == 200


async def test_no_cors_headers_unless_a_dev_origin_is_configured(rig: Rig) -> None:
    r = await rig.get("/v1/operations", headers={"Origin": "http://localhost:5173"})
    assert not any(k.lower().startswith("access-control-") for k in r.headers)
    pre = await rig.http.options(
        "/v1/operations/status",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
    )
    assert pre.status_code != 204 and "access-control-allow-origin" not in pre.headers


async def test_the_configured_dev_origin_gets_cors_and_nobody_else(
    make_rig: Callable[..., Any],
) -> None:
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
        post = await rig.post("/v1/operations/status", headers={"Origin": "http://localhost:5173"})
        assert post.status_code == 200
        bad = await rig.post("/v1/operations/status", headers={"Origin": "http://evil.example"})
        assert bad.status_code == 403


async def test_a_post_must_be_json(rig: Rig) -> None:
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
        r = await rig.http.post(
            "/v1/operations/status", content=b"{}", headers={**rig.auth, "Content-Type": ctype}
        )
        assert r.status_code == 415, ctype  # the "simple" types a form can send cross-origin


async def test_a_body_over_the_limit_is_refused(make_rig: Callable[..., Any]) -> None:
    async with make_rig(max_body_bytes=2048) as rig:
        big = {"input": {"q": "x" * 5000}}
        assert (await rig.post("/v1/operations/status", big)).status_code == 413
        assert (await rig.post("/v1/operations/status", {"input": {}})).status_code == 200


async def test_mutating_requests_are_rate_limited_per_client(
    make_rig: Callable[..., Any],
) -> None:
    async with make_rig(rate_limit_per_minute=10) as rig:
        codes = [(await rig.post("/v1/operations/status")).status_code for _ in range(40)]
        assert 200 in codes and 429 in codes
        limited = await rig.post("/v1/operations/status")
        assert limited.status_code == 429 and "retry-after" in limited.headers
        assert (await rig.get("/v1/operations")).status_code == 200, "reads are not limited"


async def test_a_wrong_token_does_not_reach_the_rate_limit_or_the_handler(rig: Rig) -> None:
    r = await rig.http.post(
        "/v1/operations/status", json={}, headers={"Authorization": "Bearer nope"}
    )
    assert r.status_code == 401


async def test_responses_carry_the_hardening_headers(rig: Rig) -> None:
    r = await rig.get("/v1/operations")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "server" not in r.headers


async def test_the_placeholder_page_is_public_and_has_a_strict_csp(rig: Rig) -> None:
    r = await rig.http.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "default-src" in r.headers["content-security-policy"]


async def test_static_files_are_served_from_the_configured_folder(
    make_rig: Callable[..., Any], tmp_path: Any
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>mine</h1>")
    async with make_rig(static_dir=str(site)) as rig:
        r = await rig.http.get("/")
        assert r.status_code == 200 and "mine" in r.text
        assert (await rig.http.get("/../etc/passwd")).status_code in (400, 404)


# -- binding ---------------------------------------------------------------------------------
def test_a_non_loopback_bind_needs_allow_remote_and_an_address() -> None:
    for host in ("0.0.0.0", "::", "192.168.1.5"):  # noqa: S104
        with pytest.raises(ValidationError):
            APISettings(host=host)  # not loopback, and allow_remote was not set
    with pytest.raises(ValidationError):
        APISettings(host="localhost", allow_remote=True)  # an address, not a name


def test_the_server_itself_refuses_a_non_loopback_host(rig: Rig) -> None:
    with pytest.raises(ConfigError):
        ApiServer(rig.api, host="0.0.0.0")  # noqa: S104


# -- files -----------------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_token_file_is_private_and_a_loose_one_is_tightened(tmp_path: Any) -> None:
    path = tmp_path / "api_token"
    token = runtime_module.load_or_create_token(path)
    assert len(token) >= 40 and path.stat().st_mode & 0o777 == 0o600
    assert runtime_module.load_or_create_token(path) == token, "stable across starts"
    path.chmod(0o644)
    assert runtime_module.load_or_create_token(path) == token
    assert path.stat().st_mode & 0o777 == 0o600
