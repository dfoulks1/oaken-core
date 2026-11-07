# 11. Service units: a dependency-ordered build/start/stop for the composition root

- Status: accepted
- Date: 2026-10-22

## Context

`oaken.daemon.core.Core` is the composition root: `open()` constructs every store and service
once, `start()` brings them up, `stop()` takes them down. Startup has a required order (settings →
databases → plugin host → jobs and sync → API, so nothing is asked of a half-started core) and
shutdown is the reverse.

Before this decision, that order lived in two hand-maintained places that had to agree:
`start()` appended stage names to a `self._started` list as it brought each up, and `stop()` held a
parallel `name → stop-handler` dict and walked `reversed(self._started)`. A stage added to one and
not the other would `KeyError` on shutdown, and the ordering was implicit in statement order rather
than declared. Adding a subsystem meant editing both methods (and the store-close tail), which is
exactly the kind of core churn this stabilization work is meant to remove
(`docs/prompts/CORE_34-stabilize-core.md`, Stage 1 §2).

## Decision

Introduce a small, **core-internal** service-unit abstraction in `oaken.daemon.units`:

- `ServiceUnit` — a `Protocol` with `name`, `async start()` and `async stop()`. `start()` raising
  aborts bring-up; `stop()` must be safe on a stage that never started.
- `unit(name, start, stop)` — builds a `ServiceUnit` from a name and two callables, so `Core` can
  declare a stage inline without a class per stage.
- `Lifecycle` — holds the ordered unit list. `start(started)` starts each unit in order, appending
  its name to the caller's `started` list as it comes up; if one raises it stops the
  already-started units (reverse) and re-raises. `stop(started)` stops the started units in reverse,
  clearing `started`, logging a failing `stop` rather than letting it stop the others.

`Core.open()` still builds the object graph — construction order encodes the dependencies and stays
explicit and readable — and then builds one `Lifecycle` from a list of units (`host`, `sync`,
`jobs`, `mcp` when enabled, `api`). `start()` and `stop()` delegate the ordering and bookkeeping to
the `Lifecycle`. The daemon keeps `self._started` (the list the `Lifecycle` appends to) and
`self._open`, because those are the observable state a half-built teardown and the daemon tests
rely on.

**This is a behaviour-neutral refactor.** The construction order, the start order, the stop order
and the stage names are identical to before. The entire existing daemon suite
(`src/oaken/daemon/tests/test_daemon.py`: startup/shutdown, failed-`open()` unwind that must not
touch attributes `open()` never assigned, SIGHUP reload, single-instance lock) passes **unmodified**.

`units` is **not** exported from `oaken.daemon.__init__` and is **not** a public seam. It is an
internal convenience so that adding a subsystem is one more `unit(...)` in `open()` instead of
edits to `start()` and `stop()` in two places.

## Consequences

- One ordered list is the single source of truth for both start and stop; a started stage can no
  longer lack a stopper.
- Adding a subsystem touches `open()` (construct it) and the unit list (one entry). `start()` and
  `stop()` do not change.
- The store-close tail (`readstate`, `index`, `raw_db`) moved into a `_close_stores()` helper that
  both `stop()` and a failed `start()` call, guarded by `_open` so it runs once and tolerates stores
  an interrupted `open()` never assigned — the property the failed-`open()` regression test pins.
- `Lifecycle` has its own focused tests (`src/oaken/daemon/tests/test_units.py`): start order,
  reverse stop, abort-and-unwind on a failing start, safety when nothing started, and a failing stop
  not blocking the rest.
- A future, larger change could make units carry their dependencies explicitly and topologically
  sort them; today the list order is the declaration, which is enough and keeps `open()` readable.
