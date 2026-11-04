# Step 1: store, change log, labels

Goal: core can persist through a store plugin, log every write, and hold labels in two layers.

Build:
1. `store.py`. A `Store` protocol. `open() -> Database`. A `SqliteStore` default that wraps
   `oaken_core.db`. Core opens through it. Nothing else opens a database.
2. `migrations/0001_core.sql`. Table `change_log`: id, ts, actor_kind, actor, subject_id, key, layer,
   op, before, after. Append-only. Enforce it with triggers that refuse update and delete.
   Table `labels`: `(subject_id, key, layer)` primary key, value, actor, at. Layer is `human` or `machine`.
3. `changelog.py`. One append function. It runs in the caller's transaction. No other writer.
4. `labels.py`. `BaseLabel.new`, `ReservedLabel.new`, `LabelMaker`, `LabelError`.
   - Reserved keys: `queue`, `priority` (urgent, high, normal, low), `confidential`.
   - A key must be registered before it is written.
   - A plugin cannot register a reserved key.
   - `queue` takes a validator hook. Step 2 fills it.
5. Label API: `set`, `get` (effective), `layers` (both), `all`, `clear_human`, `clear_machine`.
   Effective is the human value if present, else the machine value.
   Actor kind `user` writes the human layer. `agent` and `system` write the machine layer.
   Clearing the human layer is the unlock. Every set and clear logs one row.

Reuse: `db.py` (done), `actors.py` (done), `clock.py` (done), `ids.py` (done).
Not yours: queues, jobs, the plugin host. Do not build them.

Done when:
- Every label write has a log row. A test loops over writes to prove it.
- The log refuses update and delete.
- A machine write never changes the effective value while a human value exists.
- Clearing the human layer reverts to the machine value.
- Clearing the machine layer keeps the human value.
- Unknown and colliding keys raise `LabelError`.
