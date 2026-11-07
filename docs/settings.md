+++
title = "Settings, storage and the event log"
subtitle = "Change core's settings, see where the data is, tidy it, and read what happened"
lead = "Everything here is a command on the running core. It edits one table of config.toml and leaves your comments alone, and it tells you what a change needs before it takes effect."
badges = [["text-bg-warning", "Alpha"]]
group = "start"
order = 12
description = "oaken settings get and set, config validate and save, reload, storage and vacuum, the answer cache, the event log, the log and the change log."
+++

Examples use the real CLI. A change asks first: without `--yes` it prints what would change and
exits 5. Add `--json` for the original data.

## Core settings

```bash
oaken settings get                                   # the sections, with a summary each
oaken settings get --section log --json              # one section: every key, value, default, editable, effect
oaken settings set --section log --values '{"level":"DEBUG"}'          # shows the change, exit 5
oaken settings set --section log --values '{"level":"DEBUG"}' --yes    # saves it
oaken reload                                         # core re-reads config.toml in place
```

Only some keys are editable here. Anything that changes where core listens or keeps data, or that
hands text to an outside worker or a LAN backend, is set by hand in `config.toml`. `get --json`
marks it `editable: false` with the note `set by hand in config.toml`, and `set` refuses it:

```text
log.file cannot be changed here (set by hand in config.toml)
```

| Section | Table | Holds | By hand |
| --- | --- | --- | --- |
| `log` | `[log]` | Level, per-logger levels, format, keep days, `enabled`. | `file`, `file_max_bytes`, `file_backups`, `events_file` |
| `jobs` | `[jobs]` | Leases, retries, backoff, timeouts, per-kind limits, store and embed defaults. | none |
| `swarm` | `[swarm]` | Agent limits and lease time. | `enabled`, `allowed_kinds` |
| `router` | `[router]` | Shortcuts, confidence, token limits, waits, embedder and vector store. | `lan_allowlist` |
| `console` | `[console]` | `palette` and `mode`. | `port` |

`[api]`, `[queues]`, `[labels]`, `[extensions]` and each plugin's table are not here. Plugin
settings have their own commands: see [Plugins](plugins.md).

### What a change costs

Each key has an effect, and the answer to `set` names it:

- **reload**: `oaken reload`. Core rebuilds on `config.toml` in the same process, with no restart.
  Logging is reconfigured too.
- **restart**: for a key a reload cannot apply. No key needs one today, but if one does it is
  reported the same way.
- **none**: the change altered nothing, so nothing was written.

### Safe by construction

- Only the one table is rewritten. Comments, other tables and plugin settings stay.
- The whole file is validated before anything is written. An error names the key.
- A change that alters nothing writes nothing (`Nothing to change.`).
- There is no environment override of a setting. The file is the only source. (`OAKEN_HOME` only
  says where the file lives.)
- A key that looks like a secret is refused. See [Store credentials](keyring.md).

## The whole file

```bash
oaken config validate --text "$(cat config.toml)"    # ok: true, or the error; warnings name plugins that will stay disabled
oaken config save --text "$(cat config.toml)" --yes  # checked first; the old file is kept as config.toml.bak
```

`save` writes nothing for a file core would not start on. The backup is one level: the file as it
was before this save. A plugin table that does not fit its plugin loads anyway, and comes back as a
warning, because that plugin stays disabled until you fix it. The console's `/config` page does the
same as text.

## Storage

```bash
oaken storage info          # data dir, core.db and its write-ahead log, other databases there
oaken storage vacuum --yes  # queue a vacuum job
```

Sizes are read from the files. `vacuum` queues the job `maintenance.vacuum` and returns its
`job_id`. Only one runs at a time (asking again returns the one queued or running, `created:
false`). It refuses when free disk is under 1.1 times the database's size. While it works the
database is write-locked: writers wait and readers carry on. Watch it with `oaken job` (see
[Jobs](architecture/jobs.md)).

## The answer cache

```bash
oaken cache stats                         # entries, bytes, entries per analyzer, oldest
oaken cache clear --yes                   # all of it
oaken cache clear --analyzer triager.llm --yes   # one analyzer's answers
```

The cache holds model answers keyed by what produced them. Clearing costs only the calls it had
saved. See [The model router](architecture/router.md).

## What happened: events, the log and the change log

```bash
oaken events tail --limit 20                    # recent events, including from before core's last restart
oaken events tail --kinds JobStateChanged --since 2026-11-07T09:00:00
oaken watch                                     # follow events live
oaken log tail --level warning --limit 50       # the log; also --plugin KIND/NAME or --stage QUEUE
oaken log list --limit 20                       # the change log: who changed what
```

- **Events.** Core writes every event to `events.jsonl` in the data directory (`[log] events_file`;
  empty turns it off), one JSON line each, owner-only. It rotates with the log file's size
  (`file_max_bytes`) and keeps `file_backups` older files. Events hold ids, states and numbers,
  never item text. `events tail` reads them back, oldest first.
- **The log.** `log tail` reads the `log_lines` table (kept `[log] keep_days`, default 7), or the
  log file when `[log] file` is set. `kept_in` says which. Lines are scrubbed already. It scopes to
  one plugin, one stage (a queue's name), or everything.
- **The change log.** `log list` shows each operation that ran: time, actor, the operation, and a
  hash of its input (never the input, so a secret cannot land there).

## Is it healthy?

`oaken status` prints the version, each queue (queued, running, failed, paused, oldest wait) and how
many plugins are healthy. It exits 3 when core is not running.
