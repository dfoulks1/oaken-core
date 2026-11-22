# 3. SQLite access: aiosqlite with a write broker and a read pool

- Status: accepted
- Date: 2026-10-20

## Context

Core is one asyncio process that reads and writes two SQLite files while it syncs, parses,
embeds and serves clients. SQLite is synchronous, so every call must run off the event loop.
The simplest options, a single dedicated worker thread or one shared connection, serialise
everything: a slow query or a long write transaction blocks every other reader behind it.
For an interactive client sitting on top of background ingest, that is the wrong failure mode.

## Decision

Use `aiosqlite` (one thread per connection, awaitable API) and put a small broker in front of it
(`oaken.core.db.Database`):

- **One writer connection**, used through `transaction()`. SQLite permits one writer at a
  time, so writes are serialised in arrival order by an `asyncio.Lock`. Waiting is cheap and
  never blocks the event loop. Transactions start with `BEGIN IMMEDIATE`, so a write never
  fails midway with `SQLITE_BUSY`, and a block that raises or is cancelled rolls back.
- **A pool of read-only connections** (`PRAGMA query_only=ON`, default 4). In WAL mode readers
  never block the writer and the writer never blocks readers, so reads run concurrently with
  each other and with an open write transaction, seeing the last committed state.
- Every connection sets `foreign_keys=ON`, `busy_timeout` and `synchronous=NORMAL`; the file is
  switched to WAL once on open.
- Autocommit mode with explicit transactions, so the layer controls exactly when a transaction
  begins and ends. The migration runner therefore splits scripts into statements rather than
  using `executescript`, which would commit the surrounding transaction.

## Consequences

- One extra dependency (`aiosqlite`), well maintained and small.
- Reads inside a write transaction must use the `Tx` object to see their own uncommitted
  writes; plain `fetchone`/`fetchall` do not.
- Extensions (sqlite-vec, prompt 08) and `ATTACH` are applied to every connection, so the
  pool stays uniform.
- A long write transaction still delays other writers. Keep transactions short; batch work
  into many small transactions rather than one large one.
- Rejected: a single worker thread (simple, but readers queue behind writers) and a
  per-call connection (loses PRAGMA setup and pays connection cost every time).
