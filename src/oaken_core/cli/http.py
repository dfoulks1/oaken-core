# SPDX-License-Identifier: Apache-2.0
"""The HTTP :class:`~oaken_core.cli.client.ApiClient`: loopback HTTP to the running daemon.

This class is the only place in the CLI that knows the wire format. The server side is
``oaken_core.api`` and the generated reference page (*Reference -> HTTP API*) documents it:

* ``GET  /v1/health`` -- unauthenticated liveness, ``{"status": "ok"}``.
* ``GET  /v1/operations`` -- the catalogue; each entry decodes into :class:`OpInfo`.
* ``POST /v1/operations/{name}`` -- body ``{"input", "confirm", "confirmation_token"}``, header
  ``X-Oaken-Client: cli``; the answer is ``{"request_id", "status", "output", "proposal"}``.
* ``GET  /v1/events`` -- server-sent events, resumed with ``Last-Event-ID``.
* ``/v1/agent/jobs/...`` -- the agent job protocol, used by :class:`AgentClient` with an agent
  token (a different credential that opens nothing else).

Error mapping: ``401`` is exit 4 (the token was refused), a connection that cannot be made is
:class:`~oaken_core.cli.exits.CoreUnavailable` (exit 3), and any other error body ``{"code","message"}``
becomes a :class:`~oaken_core.cli.exits.CliError` whose exit code comes from
:func:`~oaken_core.cli.exits.exit_for_error_code`. Messages are the server's, which never contain private text.

``transport`` is for tests: an ``httpx2.ASGITransport`` over the real app runs the whole client
with no socket.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx2

from oaken_core.cli.client import ApiClient, CallResult, Event, OpInfo, Proposal
from oaken_core.cli.discovery import Endpoint
from oaken_core.cli.exits import (
    CliError,
    CoreUnavailable,
    ExitCode,
    exit_for_error_code,
)

CLIENT_CLASS = "cli"
CONNECT_ERRORS = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.RemoteProtocolError)
RECONNECT_TRIES = 5


def _raise_for(response: httpx2.Response) -> None:
    """Turn an error response into the CLI's own error with the right exit code."""
    if response.status_code < 400:
        return
    if response.status_code == 401:
        raise CliError(
            "the API token was refused; check the token file (or --token-file)", ExitCode.AUTH
        )
    code, message = "", f"core answered {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        code = str(body.get("code", ""))
        message = str(body.get("message") or message)
    if response.status_code == 503:
        raise CoreUnavailable(message)
    exit_code = exit_for_error_code(code)
    if response.status_code == 422 and exit_code == ExitCode.ERROR:
        exit_code = ExitCode.USAGE if code in ("", "invalid_input") else ExitCode.ERROR
    raise CliError(message, exit_code)


def _proposal(raw: Mapping[str, Any]) -> Proposal:
    return Proposal(
        operation=str(raw.get("operation", "")),
        summary=str(raw.get("summary", "")),
        changes=list(raw.get("changes", [])),
        total_changes=int(raw.get("total_changes", 0)),
        confirmation_token=str(raw.get("confirmation_token", "")),
        expires_at=str(raw.get("expires_at", "")),
    )


class _Base:
    """Shared connection handling for the two clients."""

    def __init__(
        self,
        endpoint: Endpoint,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.endpoint = endpoint
        self._http = httpx2.AsyncClient(
            base_url=endpoint.base_url,
            transport=transport,
            timeout=timeout,
            headers=self._headers(),
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.endpoint.token}",
            "X-Oaken-Client": CLIENT_CLASS,
            "Accept": "application/json",
        }

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx2.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except CONNECT_ERRORS as exc:
            raise CoreUnavailable(str(exc)) from None
        except httpx2.TimeoutException:
            raise CliError("core did not answer in time", ExitCode.ERROR) from None
        _raise_for(response)
        return response


class HttpApiClient(_Base, ApiClient):
    """Talks to the running daemon over loopback HTTP."""

    async def health(self) -> Mapping[str, Any]:
        try:
            response = await self._http.get("/v1/health")
        except CONNECT_ERRORS as exc:
            raise CoreUnavailable(str(exc)) from None
        _raise_for(response)
        body: Mapping[str, Any] = response.json()
        return body

    async def list_ops(self) -> list[OpInfo]:
        body = (await self._request("GET", "/v1/operations")).json()
        return [
            OpInfo(
                name=o["name"],
                summary=o.get("summary", ""),
                permission=o.get("permission", "read"),
                group=str(o.get("group", "")),
                plugin=o.get("plugin"),
                template=o.get("template"),
                agent_ok=bool(o.get("agent_ok", False)),
                streaming=bool(o.get("streaming", False)),
                input_schema=o.get("input_schema", {}),
                output_schema=o.get("output_schema", {}),
            )
            for o in body.get("operations", [])
        ]

    async def call(
        self,
        name: str,
        payload: Mapping[str, Any] | None = None,
        *,
        confirm: bool = False,
        confirmation_token: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> CallResult:
        body = {
            "input": dict(payload or {}),
            "confirm": confirm,
            "confirmation_token": confirmation_token,
        }
        extra: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        data = (await self._request("POST", f"/v1/operations/{name}", json=body, **extra)).json()
        proposal = data.get("proposal")
        return CallResult(
            request_id=str(data.get("request_id", "")),
            output=data.get("output"),
            proposal=_proposal(proposal)
            if data.get("status") == "confirmation_required" and proposal
            else None,
        )

    async def stream_trace(self, filters: dict[str, Any]) -> AsyncIterator[Event]:
        params: list[tuple[str, str | int | float | bool | None]] = [
            (k, str(filters[k])) for k in ("plugin", "job_id", "subject") if filters.get(k)
        ]
        params += [("kind", k) for k in filters.get("kinds") or []]
        try:
            async with self._http.stream(
                "GET",
                "/v1/trace/stream",
                params=params,
                headers={"Accept": "text/event-stream"},
                timeout=httpx2.Timeout(10.0, read=None),
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    _raise_for(response)
                async for event in _parse_sse(response):
                    yield event
        except (*CONNECT_ERRORS, httpx2.ReadError):
            raise CoreUnavailable("the trace stream dropped") from None

    async def stream_events(self, last_event_id: str | None = None) -> AsyncIterator[Event]:
        """Events as they arrive, reconnecting with ``Last-Event-ID`` if the stream drops. A
        ``gap`` event is yielded like any other: it means "re-fetch state"."""
        failures = 0
        while True:
            headers = {"Accept": "text/event-stream"}
            if last_event_id:
                headers["Last-Event-ID"] = last_event_id
            try:
                async with self._http.stream(
                    "GET", "/v1/events", headers=headers, timeout=httpx2.Timeout(10.0, read=None)
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        _raise_for(response)
                    failures = 0
                    async for event in _parse_sse(response):
                        if event.id:
                            last_event_id = event.id
                        yield event
                    return  # the server ended the stream on purpose (it is shutting down)
            except (*CONNECT_ERRORS, httpx2.ReadError):
                failures += 1
                if failures > RECONNECT_TRIES:
                    raise CoreUnavailable(
                        "the event stream dropped and did not come back"
                    ) from None
                await asyncio.sleep(min(failures, 3))


async def _parse_sse(response: httpx2.Response) -> AsyncIterator[Event]:
    """Server-sent events: ``id:``/``event:``/``data:`` lines, a blank line ends one event, and a
    line starting with ``:`` is a comment (the heartbeat)."""
    event_id, name = "", "message"
    data: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data:
                yield Event(id=event_id, event=name, data=_json("\n".join(data)))
            event_id, name, data = "", "message", []
        elif line.startswith(":"):
            continue
        else:
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "id":
                event_id = value
            elif field == "event":
                name = value
            elif field == "data":
                data.append(value)


def _json(text: str) -> Mapping[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        return {"raw": text}
    return value if isinstance(value, dict) else {"value": value}


class AgentClient(_Base):
    """The agent job protocol, with an *agent* token. It can reach nothing but ``/v1/agent/``."""

    async def _post(self, path: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        data: dict[str, Any] = (await self._request("POST", path, json=dict(body or {}))).json()
        return data

    async def claim(
        self, kinds: list[str] | None = None, max_chars: int | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"kinds": kinds} if kinds else {}
        if max_chars:
            body["max_chars"] = max_chars
        return await self._post("/v1/agent/jobs/claim", body)

    async def heartbeat(self, job_id: str) -> dict[str, Any]:
        return await self._post(f"/v1/agent/jobs/{job_id}/heartbeat")

    async def complete(
        self, job_id: str, result: Mapping[str, Any] | None, produced_by: str | None
    ) -> dict[str, Any]:
        return await self._post(
            f"/v1/agent/jobs/{job_id}/complete", {"result": result, "produced_by": produced_by}
        )

    async def fail(
        self, job_id: str, error: str, *, permanent: bool = False, retry: bool = False
    ) -> dict[str, Any]:
        return await self._post(
            f"/v1/agent/jobs/{job_id}/fail",
            {"error": error, "permanent": permanent, "retry": retry},
        )
