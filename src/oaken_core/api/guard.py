# SPDX-License-Identifier: Apache-2.0
"""The front door: every request passes these checks, in this order, before any route runs.

    1. Host header is ``127.0.0.1:<port>`` or ``localhost:<port>``     -> 400   (DNS rebinding)
    2. a CORS preflight from the one configured dev origin is answered; nothing else is
    3. a state-changing request with a foreign Origin                  -> 403   (cross-site forgery)
    4. a state-changing request that is not ``application/json``       -> 415
    5. the bearer token of the surface the path belongs to             -> 401
    6. a mutating request over its client's rate                       -> 429
    7. a body over the surface's limit                                 -> 413
    8. a surface already running its concurrency limit                 -> 429

``/v1/health`` and the static UI are the only paths without a token, and still get 1 to 4. A
*surface* is a path prefix with its own token and limits: the general API, the agent routes, and
the MCP doors (the person's and an agent's). Which surface a path is on decides the caller class,
never a header, so a token for one surface is worthless on another.
"""

import asyncio
import ipaddress
import json
from collections.abc import Awaitable, Callable, Collection, MutableMapping
from dataclasses import dataclass, field
from typing import Any

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from oaken_core.api.errors import body as error_body
from oaken_core.api.ratelimit import RateLimiter
from oaken_core.logging import get_logger

log = get_logger("oaken_core.api.guard")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PUBLIC_HEALTH = "/v1/health"
STREAM_PATHS = frozenset({"/v1/events"})  # long-lived: not counted against concurrency
CORS_HEADERS = "Authorization, Content-Type, X-Oaken-Client, Last-Event-ID"

Authenticate = Callable[[str], Awaitable[Any | None]]


@dataclass
class Surface:
    """One door: a path prefix, how to check its token, and its own limits."""

    name: str
    prefix: str
    authenticate: Authenticate  # token -> principal, or None if it is not valid here
    max_body: int
    max_concurrent: int = 32
    client: str | None = None  # fixed caller class for this door (``mcp``); None = from header
    _slots: asyncio.Semaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._slots = asyncio.Semaphore(self.max_concurrent)

    def owns(self, path: str) -> bool:
        return path == self.prefix.rstrip("/") or path.startswith(self.prefix)


class Policy:
    """What the guard enforces. ``bind`` is called once the real port is known."""

    def __init__(
        self,
        *,
        dev_origin: str = "",
        limiter: RateLimiter,
        public_csp: str,
        extra_hosts: Collection[str] = (),
    ) -> None:
        self.dev_origin = dev_origin
        self.extra_hosts = tuple(h.lower() for h in extra_hosts)
        self.limiter = limiter
        self.public_csp = public_csp
        self.hosts: set[str] = set()
        self.origins: set[str] = {dev_origin} if dev_origin else set()
        self.surfaces: list[Surface] = []
        self.names: Callable[[], frozenset[str]] = frozenset  # friendly *.localhost hosts a plugin adds (BaseFriendlyNames)
        self.port = 0

    def friendly(self) -> set[str]:
        return {f"{n}:{self.port}" for n in self.names() if n.endswith(".localhost") and ":" not in n}

    def bind(self, port: int) -> None:
        names = (
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            *(f"{h}:{port}" for h in self.extra_hosts),
        )
        self.port = port
        self.hosts = set(names)
        self.origins = {f"http://{n}" for n in names} | (
            {self.dev_origin} if self.dev_origin else set()
        )

    def surface_for(self, path: str) -> Surface | None:
        # Longest prefix wins, so ``/v1/agent/`` is never read as part of ``/v1/``.
        owned = [s for s in self.surfaces if s.owns(path)]
        return max(owned, key=lambda s: len(s.prefix), default=None)


async def _send_json(
    send: Send, status: int, code: str, message: str, extra: list[tuple[bytes, bytes]] | None = None
) -> None:
    payload = json.dumps(error_body(code, message)).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
        *(extra or []),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


def _is_local(scope: Scope) -> bool:
    """True if the TCP peer is on this machine. No peer (Unix socket, test rig) counts as local."""
    client = scope.get("client")
    if not client:
        return True
    try:
        return ipaddress.ip_address(client[0]).is_loopback
    except ValueError:
        return False


def bearer(headers: Headers) -> str | None:
    value = headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


class Guard:
    def __init__(self, app: ASGIApp, policy: Policy) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":  # no WebSocket here: close it
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        await self._http(scope, receive, send)

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        p = self.policy
        headers = Headers(scope=scope)
        method: str = scope["method"]
        path: str = scope["path"]
        origin = headers.get("origin")

        host = headers.get("host", "").lower()
        friendly = p.friendly()
        if host not in p.hosts and host not in friendly:
            await _send_json(send, 400, "bad_host", "this host name is not allowed")
            return

        cors = bool(origin) and origin == p.dev_origin
        if method == "OPTIONS" and cors and "access-control-request-method" in headers:
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [
                        (b"access-control-allow-origin", (origin or "").encode()),
                        (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
                        (b"access-control-allow-headers", CORS_HEADERS.encode()),
                        (b"access-control-max-age", b"600"),
                        (b"vary", b"Origin"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return

        mutating = method not in SAFE_METHODS
        # A console form carries its own token (the real defense) and browsers may send
        # ``Origin: null`` on a same-site form post, so that one value is let through there.
        form_post = path.startswith("/ui/") and origin == "null"
        if mutating and (
            (origin is not None and origin not in p.origins and origin.removeprefix("http://") not in friendly and not form_post)
            or headers.get("sec-fetch-site") == "cross-site"
        ):
            log.warning(
                "refused cross-origin request",
                path=path,
                origin=origin,
                fetch_site=headers.get("sec-fetch-site"),
            )
            await _send_json(send, 403, "bad_origin", "cross-origin requests are not allowed")
            return
        if mutating:
            ctype = headers.get("content-type", "").split(";")[0].strip().lower()
            console_form = path.startswith("/ui/") and ctype == "application/x-www-form-urlencoded"
            if ctype != "application/json" and not console_form:  # console pages post forms
                await _send_json(send, 415, "unsupported_media_type", "send application/json")
                return

        surface = p.surface_for(path)
        principal: Any = None
        is_public = path == PUBLIC_HEALTH or (surface is None and not path.startswith("/v1/"))
        if is_public and path != PUBLIC_HEALTH and not _is_local(scope):
            # The console pages carry no token: they are for this machine only, however the
            # listener is bound. A remote client gets the token-protected API and nothing else.
            await _send_json(send, 403, "local_only", "this page is only served to this machine")
            return
        if not is_public:
            if surface is None:
                surface = p.surface_for("/v1/")  # an unknown /v1 path is still a 401 first
            token = bearer(headers)
            principal = await surface.authenticate(token) if surface and token else None
            if principal is None:
                await _send_json(
                    send, 401, "unauthorized", "a valid bearer token is required",
                    [(b"www-authenticate", b"Bearer")],
                )  # fmt: skip
                return
        assert surface is not None or is_public  # noqa: S101

        if mutating and surface is not None:
            who = headers.get("x-oaken-client", "") if surface.client is None else surface.client
            key = f"{surface.name}:{getattr(principal, 'id', '')}:{who[:32]}"
            wait = p.limiter.take(key)
            if wait > 0:
                await _send_json(
                    send, 429, "rate_limited", "too many requests",
                    [(b"retry-after", str(max(1, round(wait))).encode())],
                )  # fmt: skip
                return

        scope["oaken"] = {
            "surface": surface.name if surface else "public",
            "principal": principal,
            "client": surface.client if surface else None,
        }
        wrapped = self._decorate(send, path, origin if cors else None)

        if surface is None or is_public:
            await self.app(scope, receive, wrapped)
            return

        if mutating or headers.get("content-length"):
            replay = await self._read_body(surface, headers, receive, wrapped)
            if replay is None:
                return
            receive = replay
        if path in STREAM_PATHS:
            await self.app(scope, receive, wrapped)
            return
        if surface._slots.locked():
            await _send_json(wrapped, 429, "busy", "too many requests in flight on this door")
            return
        async with surface._slots:
            await self.app(scope, receive, wrapped)

    @staticmethod
    async def _read_body(
        surface: Surface, headers: Headers, receive: Receive, send: Send
    ) -> Receive | None:
        """Read the body, at most ``surface.max_body`` bytes, and hand the app a ``receive`` that
        replays it. ``None`` means the request was refused and answered."""
        declared = headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > surface.max_body):
            await _send_json(send, 413, "too_large", f"at most {surface.max_body} bytes")
            return None
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            size += len(message.get("body", b""))
            if size > surface.max_body:
                await _send_json(send, 413, "too_large", f"at most {surface.max_body} bytes")
                return None
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        data = b"".join(chunks)
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": data, "more_body": False}
            return await receive()

        return replay

    def _decorate(self, send: Send, path: str, cors_origin: str | None) -> Send:
        """Add the response headers every answer carries."""
        static = not path.startswith("/v1/")

        async def out(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                have = {k.lower() for k, _ in message["headers"]}
                add = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cross-origin-resource-policy", b"same-origin"),
                    (b"x-frame-options", b"DENY"),
                ]
                if static:
                    add.append((b"content-security-policy", self.policy.public_csp.encode()))
                else:
                    add.append((b"cache-control", b"no-store"))
                if cors_origin:
                    add += [
                        (b"access-control-allow-origin", cors_origin.encode()),
                        (b"vary", b"Origin"),
                    ]
                message["headers"] = [*message["headers"], *(h for h in add if h[0] not in have)]
            await send(message)

        return out
