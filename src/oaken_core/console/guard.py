# SPDX-License-Identifier: Apache-2.0
"""The console's guard. There is no login: the console is for this machine, so the guard asks
only that a request really is from this machine and really is from the console's own pages.

Adapted from oaken-mail ``api/guard.py`` (cf3b187): the Host check, the cross-origin refusal and
the console's form-post allowance. Dropped: bearer tokens, rate limits and body surfaces (those
guard the API, not the console). Added: the TCP peer must be loopback, and every response carries a
CSP with no script at all (ADR 0018: core owns the chrome).
"""

import ipaddress
import json
from collections.abc import Callable

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

SAFE = frozenset({"GET", "HEAD"})
CSP = "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
FORM = "application/x-www-form-urlencoded"
MAX_BODY = 256_000


def loopback(scope: Scope) -> bool:
    """The TCP peer is on this machine. No peer (a test rig) counts as local, as in the API guard."""
    client = scope.get("client")
    if not client:
        return True
    try:
        return ipaddress.ip_address(client[0]).is_loopback
    except ValueError:
        return False


class ConsoleGuard:
    def __init__(self, app: ASGIApp, port: "PortRef", names: Callable[[], frozenset[str]] = frozenset) -> None:
        self.app, self.port, self.names = app, port, names  # names: friendly hosts a plugin adds (BaseConsoleName)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return  # no websockets, no lifespan: the console is plain HTTP
        port = self.port.value
        headers = Headers(scope=scope)
        origin, method = headers.get("origin"), scope["method"]
        if not loopback(scope):
            return await self._refuse(send, 403, "local_only", "the console is for this machine only")
        # only *.localhost may be added: a browser resolves those to this machine itself, so they cannot be rebound
        hosts = {"127.0.0.1", "localhost"} | {n for n in self.names() if n.endswith(".localhost") and ":" not in n}
        if headers.get("host", "").lower() not in {f"{h}:{port}" for h in hosts}:
            return await self._refuse(send, 400, "bad_host", "this host name is not allowed")
        if method not in SAFE:
            # ``Origin: null`` is what a browser may send on a same-site form post; the CSRF token
            # is the real defense there. Any other origin must be the console itself.
            own = {f"http://{h}:{port}" for h in hosts}
            if (origin is not None and origin != "null" and origin not in own) or headers.get("sec-fetch-site") == "cross-site":
                return await self._refuse(send, 403, "bad_origin", "cross-origin requests are not allowed")
            if headers.get("content-type", "").split(";")[0].strip().lower() != FORM:
                return await self._refuse(send, 415, "unsupported_media_type", "the console posts forms")
            if int(headers.get("content-length") or 0) > MAX_BODY:
                return await self._refuse(send, 413, "too_large", "request body too large")

        async def out(message: dict) -> None:  # type: ignore[type-arg]
            if message["type"] == "http.response.start":
                h = list(message.get("headers", []))
                h += [(b"content-security-policy", CSP.encode()), (b"x-content-type-options", b"nosniff"),
                      (b"referrer-policy", b"no-referrer"), (b"cache-control", b"no-store")]  # fmt: skip
                message = {**message, "headers": h}
            await send(message)

        await self.app(scope, receive, out)

    @staticmethod
    async def _refuse(send: Send, status: int, code: str, message: str) -> None:
        body = json.dumps({"code": code, "message": message}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})  # fmt: skip
        await send({"type": "http.response.body", "body": body})


class PortRef:
    """The console's port, known only once the socket is bound."""

    def __init__(self, value: int = 0) -> None:
        self.value = value
