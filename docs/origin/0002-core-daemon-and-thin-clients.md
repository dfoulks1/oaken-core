# 2. One core daemon with thin clients

- Status: accepted (the MCP part is superseded by ADR 0004)
- Date: 2026-10-20

## Context

Oaken Mail needs durable background work (sync, embedding, enrichment) and several
front ends (CLI, MCP). Duplicating logic in each client would be fragile.

## Decision

A single long-running asyncio core process owns storage, the job queue, the ingest
pipeline and the plugin host. The CLI and an MCP stdio shim are thin clients of a
local HTTP + SSE API bound to `127.0.0.1` with token authentication. Storage is two
SQLite files: `raw.db` (source of truth: raw messages, cursors, jobs, audit log) and
`index.db` (everything derived, rebuildable from `raw.db`). Providers, embedders, LLM
backends, enrichers and vector stores are plugins that interact with core only through
declared contracts; core never imports provider code.

The full set of working rules lives in `docs/prompts/00-context.md`.

## Consequences

Clients stay small and testable, and core can run in-process with fakes for tests. The
user must start the daemon; clients do not auto-start it. Changing the embedding model
means re-running a job, never re-downloading mail.
