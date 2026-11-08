# SPDX-License-Identifier: Apache-2.0
"""The HTTP API: a thin skin over the operation registry.

Adapted from oaken-mail ``api/app.py`` (cf3b187), without the console pages (step 6) and the mail
routes. There is no route per operation. ``POST /v1/operations/{name}`` looks the name up in the
registry and calls ``CoreClient``, so the gate, the exposure rules and the audit row are the ones
every door gets. The routes here only authenticate (``guard``), pick the caller class, and turn
errors into ``{"code", "message"}``.

The agent routes (``/v1/agent/jobs/...``) take an agent token and call the ``job_*`` operations as
that agent: HTTP, the CLI and MCP are one implementation.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from oaken_core.agents import TOKEN_PREFIX, Agent
from oaken_core.api import models
from oaken_core.api.errors import INTERNAL, ApiError, body, status_for
from oaken_core.api.guard import Guard, Policy, Surface
from oaken_core.api.ratelimit import RateLimiter
from oaken_core.api.ring import EventRing
from oaken_core.api.runtime import same_secret
from oaken_core.errors import OakenError, PermissionDenied, Unavailable
from oaken_core.events import TraceEvent
from oaken_core.logging import get_logger
from oaken_core.operations.caller import CallerContext
from oaken_core.operations.client import CallResult, CoreClient
from oaken_core.operations.openapi import build_openapi
from oaken_core.operations.registry import AnyOperation

log = get_logger("oaken_core.api")

PUBLIC_CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'"
SSE_HEADERS = {"cache-control": "no-store", "x-accel-buffering": "no"}

Handler = Callable[[Request], Awaitable[Response]]


def _json(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return _json(body(code, message), status)


def safe(fn: Handler) -> Handler:
    """Turn what a handler raises into the error body. Core's own errors keep their code; anything
    else is ``internal`` and logged by type only, because its text could quote private data."""

    async def wrapped(request: Request) -> Response:
        try:
            return await fn(request)
        except ApiError as exc:
            return _error(exc.status, exc.code, exc.message)
        except OakenError as exc:
            return _error(status_for(exc), exc.code, exc.message)
        except Exception as exc:  # reduced to its type: the message may hold private text
            log.exception("request failed", path=request.url.path, error=type(exc).__name__)
            return _json(INTERNAL, 500)

    return wrapped


async def read_json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        raise ApiError(400, "bad_json", "the body is not valid JSON") from None
    if not isinstance(data, dict):
        raise ApiError(400, "bad_json", "the body must be a JSON object")
    return data


M = TypeVar("M", bound=BaseModel)


def parse(model: type[M], data: dict[str, Any]) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:  # where and what, never the value
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'body'}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)[:5]
        )
        raise ApiError(422, "invalid_input", problems) from None


def caller_of(request: Request) -> CallerContext:
    """The registry's caller class. The surface the path is on can fix it (the MCP doors do);
    otherwise a person's token may *lower* it with ``X-Oaken-Client``. ``internal`` can never be
    asked for, and a header can never raise ``mcp`` to a person."""
    info = request.scope["oaken"]
    if info["client"] == "mcp":
        return CallerContext.mcp("mcp")
    claim = request.headers.get("x-oaken-client", "cli").strip().lower()
    if claim == "cli":
        return CallerContext.cli()
    if claim == "ui":
        return CallerContext.ui()
    if claim == "mcp":
        return CallerContext.mcp("api")
    raise ApiError(400, "bad_client", "X-Oaken-Client must be cli, ui or mcp")


def operation_info(op: AnyOperation) -> models.OperationInfo:
    return models.OperationInfo(
        name=op.name, summary=op.summary, permission=op.permission, group=op.group,
        plugin=op.plugin, template=op.template, agent_ok=op.agent_ok, streaming=op.streaming, mcp=op.mcp,
        input_schema=op.input_schema(), output_schema=op.output_schema(),
    )  # fmt: skip


def call_body(op: AnyOperation, result: CallResult) -> dict[str, Any]:
    out: dict[str, Any] = {
        "request_id": result.request_id,
        "status": "ok" if result.executed else "confirmation_required",
        "output": result.output.model_dump(mode="json") if result.output is not None else None,
    }
    if op.permission == "mutate":
        out["proposal"] = result.proposal.model_dump(mode="json") if result.proposal else None
    return out


def want_from(**filters: str | None) -> dict[str, str]:
    return {k: v for k, v in filters.items() if v}


def matches(data: dict[str, Any], want: dict[str, str]) -> bool:
    """A trace row matches when each wanted field equals the row's (the context the emitter ran
    under: plugin, job id, subject)."""
    return all(str(data.get(k)) == v for k, v in want.items())


class Api:
    """The ASGI application. ``bind`` it to the port once known; ``mount`` another door."""

    def __init__(
        self,
        client: CoreClient,
        *,
        token: str,
        rate_now: Callable[[], float] | None = None,
        started_at: Any = None,
    ) -> None:
        self.client = client
        self._token = token
        self._started_at = started_at
        svc = client.svc
        cfg = svc.settings.api
        self.cfg = cfg
        limiter = RateLimiter(cfg.rate_limit_per_minute, now=rate_now or time.monotonic)
        self.policy = Policy(
            dev_origin=cfg.dev_origin, limiter=limiter, public_csp=PUBLIC_CSP,
            extra_hosts=[*cfg.allowed_hosts, cfg.host],
        )  # fmt: skip
        agent_body = svc.settings.jobs.max_result_bytes + 64 * 1024
        self.policy.names = lambda: svc.host.api_names() if svc.host else frozenset()
        self.policy.surfaces = [
            Surface("general", "/v1/", self._general, cfg.max_body_bytes, max_concurrent=64),
            Surface("agent", "/v1/agent/", self._agent, agent_body, max_concurrent=16),
        ]
        self.ring = EventRing(cfg.event_ring_size)
        self._closing = False
        self._detach: Callable[[], None] | None = None
        self.routes: list[BaseRoute] = self._routes()
        self.starlette = Starlette(routes=self.routes, exception_handlers={HTTPException: self._http_error})
        self.app: ASGIApp = Guard(self.starlette, self.policy)

    # -- lifecycle ---------------------------------------------------------------------------
    def bind(self, port: int) -> None:
        """Allow the Host and Origin values for the port core really listens on."""
        self.policy.bind(port)

    def start(self) -> None:
        if self.client.svc.bus is not None and self._detach is None:
            self._detach = self.ring.attach(self.client.svc.bus)

    def stop(self) -> None:
        """Close the event streams, so shutting the server down does not wait on them."""
        if self._detach is not None:
            self._detach()
            self._detach = None
        self.ring.close()
        self._closing = True  # open trace streams end at their next beat

    def mount(self, surface: Surface, app: ASGIApp) -> None:
        """Serve ``app`` at ``surface.prefix`` behind the same guard, with its own token and limits.
        The MCP doors mount here; the surface, not a header, sets the caller's class."""
        self.policy.surfaces.append(surface)
        # A ``Route`` for exactly this path, not a ``Mount``: a Mount does not match its own bare
        # path, and a door has no subpaths.
        self.starlette.router.routes.insert(0, Route(surface.prefix.rstrip("/"), app))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)

    # -- authentication ----------------------------------------------------------------------
    async def _general(self, token: str) -> str | None:
        return "user" if same_secret(token, self._token) else None

    async def _agent(self, token: str) -> Agent | None:
        agents = self.client.svc.agents
        if agents is None or not token.startswith(TOKEN_PREFIX):
            return None
        try:
            return await agents.authenticate(token)
        except PermissionDenied:
            return None

    # -- routes ------------------------------------------------------------------------------
    def _routes(self) -> list[BaseRoute]:
        return [
            Route("/v1/health", safe(self.health), methods=["GET"]),
            Route("/v1/operations", safe(self.list_operations), methods=["GET"]),
            Route("/v1/operations/{name}", safe(self.call_operation), methods=["POST"]),
            Route("/v1/openapi.json", safe(self.openapi), methods=["GET"]),
            Route("/v1/events", self.events, methods=["GET"]),
            Route("/v1/trace/stream", self.trace_stream, methods=["GET"]),
            Route("/v1/agent/jobs/claim", safe(self.claim), methods=["POST"]),
            Route("/v1/agent/jobs/{job_id}/heartbeat", safe(self.heartbeat), methods=["POST"]),
            Route("/v1/agent/jobs/{job_id}/complete", safe(self.complete), methods=["POST"]),
            Route("/v1/agent/jobs/{job_id}/fail", safe(self.fail), methods=["POST"]),
        ]

    @staticmethod
    async def _http_error(request: Request, exc: Exception) -> Response:
        status = exc.status_code if isinstance(exc, HTTPException) else 500
        return _error(status, "not_found" if status == 404 else "http_error", "not found" if status == 404 else "request refused")

    async def health(self, request: Request) -> Response:
        return _json(models.Health().model_dump())

    async def list_operations(self, request: Request) -> Response:
        ops = await self.client.visible(caller_of(request))
        return _json(models.OperationList(operations=[operation_info(o) for o in ops]).model_dump(mode="json"))

    async def openapi(self, request: Request) -> Response:
        return _json(build_openapi(self.client.registry.all(), version=self.client.svc.version))

    async def call_operation(self, request: Request) -> Response:
        name = request.path_params["name"]
        caller = caller_of(request)
        req = parse(models.CallRequest, await read_json(request))
        op = self.client.registry.get(name)
        result = await self.client.call(
            name, req.input, caller, confirm=req.confirm, confirmation_token=req.confirmation_token
        )
        return JSONResponse(call_body(op, result), headers={"x-request-id": result.request_id})

    async def events(self, request: Request) -> Response:
        raw = request.headers.get("last-event-id", "")
        last = int(raw) if raw.isdigit() else None
        stream = self.ring.stream(last, self.cfg.heartbeat_seconds)
        return StreamingResponse(stream, media_type="text/event-stream", headers=SSE_HEADERS)

    async def trace_stream(self, request: Request) -> Response:
        """The trace as it happens (``trace -f``): server-sent events, filtered by plugin, job or
        subject, pushed when an event is emitted. Authenticated like every ``/v1`` route; the trace
        holds private text, so it is never on a token-free page."""
        params = request.query_params
        want = want_from(plugin=params.get("plugin"), job_id=params.get("job_id"), subject=params.get("subject"))
        kinds = set(params.getlist("kind"))
        bus = self.client.svc.bus
        if bus is None or self.client.svc.tracer is None:
            raise Unavailable("there is no trace in this core")
        stream = bus.stream(lambda e: isinstance(e, TraceEvent) and matches(e.data, want) and (not kinds or e.kind in kinds))

        async def frames() -> AsyncIterator[str]:
            yield ": connected\n\n"
            try:
                while not self._closing:
                    try:
                        event = await asyncio.wait_for(stream.__anext__(), self.cfg.heartbeat_seconds)
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    except StopAsyncIteration:
                        return
                    if isinstance(event, TraceEvent):
                        row = {"ts": event.ts.isoformat(), "kind": event.kind, "data": event.data}
                        yield f"event: trace\ndata: {json.dumps(row, default=str)}\n\n"
            finally:
                await stream.aclose()

        return StreamingResponse(frames(), media_type="text/event-stream", headers=SSE_HEADERS)

    # -- the agent surface (a separate token; opens nothing else) ----------------------------
    async def _as_agent(self, request: Request, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Run a ``job_*`` operation as this agent: the same operations (and rules) as the MCP
        agent door, so the HTTP routes and the CLI are not a second implementation."""
        agent: Agent = request.scope["oaken"]["principal"]
        caller = CallerContext.mcp(agent.label, agent_id=str(agent.id))
        result = await self.client.call(name, payload, caller)
        out: dict[str, Any] = result.output.model_dump(mode="json") if result.output is not None else {}
        return out

    async def claim(self, request: Request) -> Response:
        return _json(await self._as_agent(request, "job_claim", await read_json(request)))

    async def heartbeat(self, request: Request) -> Response:
        return _json(await self._as_agent(request, "job_heartbeat", {"job_id": request.path_params["job_id"]}))

    async def complete(self, request: Request) -> Response:
        data = {**await read_json(request), "job_id": request.path_params["job_id"]}
        return _json(await self._as_agent(request, "job_complete", data))

    async def fail(self, request: Request) -> Response:
        data = {**await read_json(request), "job_id": request.path_params["job_id"]}
        return _json(await self._as_agent(request, "job_fail", data))
