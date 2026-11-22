# 7. Core stays headless; a UI is a separate client

- Status: accepted
- Date: 2026-10-20

## Context

ADR 0002 said core hosts the UI. Keeping core as the thing that also serves a browser pulls
two unrelated concerns into one process: the long-running mail pipeline (sync, ingest, jobs, the
scheduler, the operation registry) and the delivery of HTML/CSS/JS to a browser. We want core to be
a **pipeline with an API**, matching how `oaken-cli` already treats it: a thin client over the HTTP
bridge (ADR 0005), never importing core internals beyond the shared path/version helpers.

## Decision

- **Core is headless.** It exposes the general API (`/v1/*`) and the MCP surface. Its only pages
  are its own console (`/`, `/config`, `/jobs`, `/stats`).
- **A UI is a separate client**, outside this repository, that reaches core only through `/v1/*`
  (the general API bridge the CLI uses), never the MCP surface.
- **Core does not start a UI and a UI does not start core.**

## Consequences

- Every mail action stays a registry operation behind the confirm gate and the audit log; a client
  cannot bypass them.
- Core's `[api] static_dir` mount is vestigial and deprecated; a later change may remove it.
- A second or replacement UI needs no change to core.
