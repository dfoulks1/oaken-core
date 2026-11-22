# 1. Record architecture decisions

- Status: accepted
- Date: 2026-10-20

## Context

Oaken Mail is built in small, documented steps. Design decisions need a durable,
reviewable record so contributors can see why things are the way they are.

## Decision

We record significant architecture decisions as short Markdown files in `docs/adr/`,
numbered sequentially (`NNNN-title.md`), each with a status and date. A decision is
superseded by a new ADR, never silently rewritten.

## Consequences

Decisions are discoverable and reviewable in pull requests. Writing an ADR is a small
recurring cost for any change that alters the architecture.
