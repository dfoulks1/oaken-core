# 5. The HTTP stack and how the local API defends itself

- Status: accepted
- Date: 2026-10-20

## Context

ADR 0002 put the API in core and ADR 0004 put MCP on the same listener. Prompt 16 has to pick
the server, and the choice matters less than the threat: a loopback port is reachable by every
process on the machine and, through a user's browser, by every web page they open. The API can
read all of someone's mail and change its state. "Only listens on localhost" is not a defence on
its own.

## Decision

**Starlette served by uvicorn, run inside core's event loop.** The operation registry already
generates its own OpenAPI document from pydantic models (`build_openapi`), so FastAPI would add
a second schema generator, dependency injection and about forty transitive packages for nothing
we use. Starlette gives routing, streaming responses and static files in a few thousand lines;
uvicorn is a small pure-Python ASGI server (with `h11`). Both are permissively licensed (BSD).
Core opens the socket itself and hands it to uvicorn, so it knows the real port before the first
request and a second core cannot take the same port.

**Routes are generated, not written.** `/v1/operations` lists the registry and
`POST /v1/operations/{name}` calls it through `CoreClient`, the one path that checks exposure,
validates input, applies the permission class and confirm gate, runs the handler and audits. The
only hand-written routes are not operations: health, the OpenAPI document, the event stream, the
agent job protocol, and the static UI.

**Defences, in the order a request meets them** (all in one pure-ASGI guard, so nothing reaches a
route without passing it):

1. Loopback only. Configuration cannot ask for another address (the setting is a literal), and
   the server refuses one if constructed with it.
2. `Host` must be `127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding).
3. Cross-origin preflight is answered only for the one configured development origin; otherwise
   there are no CORS headers at all.
4. A state-changing request with a foreign `Origin` or `Sec-Fetch-Site: cross-site` is refused,
   even with a valid token.
5. A `POST` must be `application/json`. A form or `text/plain` cross-site request cannot set that
   type without a preflight, which step 3 refuses.
6. A bearer token for the surface being addressed, compared in constant time (SHA-256 of both,
   then `hmac.compare_digest`).
7. A per-client token bucket on mutating requests, a body size limit, and a concurrency limit per
   surface, so one caller cannot starve another.

Only `/v1/health` is open, and it says `{"status": "ok"}` and nothing else.

**Surfaces.** A surface is a path prefix with its own token, size limit, concurrency limit and
caller class: the general API (`/v1/`), the agent protocol (`/v1/agent/`), and, in prompt 18,
`/mcp`. A token is checked only against the surface its path is on, so an agent token opens no
general route and the general token opens no agent route. The surface fixes the caller class for
a mount. On the general surface the client may *lower* its privilege with `X-Oaken-Client`
(`mcp` forces the confirm flow); nothing can raise it, and `internal` cannot be asked for.

**Secrets on disk.** The API token is 256 random bits created on first start. It and the runtime
file are created `0600` from the first byte (not written and then chmodded). The runtime file
(pid, port, version, start time) is also the single-instance lock: created exclusively, replaced
if its process is gone, and removed on a clean shutdown by the process that owns it.

**Events.** `GET /v1/events` is server-sent events numbered by an in-memory ring (default
1000). `Last-Event-ID` resumes from the ring; an id older than the ring, or from an earlier run,
gets a `gap` event so the client knows to re-fetch. Event payloads carry ids and counts, never
message text. Requests are never logged (a URL can carry a search).

## Consequences

- The OpenAPI document and the reference page are produced from the registry, so a new operation
  appears over HTTP, in the CLI and in MCP with no per-surface work.
- Two tokens exist already, and a third arrives with MCP. Each is separate on purpose; losing the
  general token does not give an agent's powers or the reverse.
- A browser tab cannot call the API. A local malicious *process* running as the same user can read
  the `0600` token file and so can do what the user can; that is outside this threat model, and
  the audit log still records what it did.
- The ring is in memory, so a restart of core is a `gap` for every client. That is intended:
  clients re-sync from the operations, which are the source of truth.
- uvicorn and Starlette are new dependencies. No GPL code is added.
