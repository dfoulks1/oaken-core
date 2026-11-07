# Oaken Core

An async pipeline and a plugin loader. It has no nouns: no message, no person, no file. What an item *is* comes from the plugins
you load, so the same core runs a mail client ([Oaken Mail](https://github.com/dfoulks1/oaken-mail)), a CRM, or whatever you build.

**Alpha.** It works and it is used, but expect rough edges and change.

```bash
pip install oaken-core
oaken start                    # the daemon, in the background
oaken plugins                  # what you can add from oaken-plugins
oaken plugin add KIND.NAME     # for example: oaken plugin add formatter.ini
```

The console is at <http://127.0.0.1:62536/> by default. It listens on this machine only.

## How it works

An item goes **Intake** (a provider submits it; a formatter splits it into named parts) then **triage** (triagers decide before anything
is stored, and may drop it) then **store** (an ingestor keeps what was accepted). Labels, queues, jobs, a scheduler and a model router
sit alongside. Everything is a plugin of some kind: provider, formatter, triager, enricher, action, ingestor, store, embedder, LLM backend and
more. Core ships the pipeline, the host, a console, a CLI (`oaken`), an HTTP API and an MCP door.

- **Plugins** are classes with a version, the plugin API they speak and the core versions they need. A **template** plugin can make
  several instances (one per account, per agent). Plugins carry **tags** for what they are for: `oaken plugins --kind provider --tag mail`.
- **Secrets** (a password, a token) live only in the OS keyring, never in `config.toml`.
- **Confidential** items never reach a hosted model: the router refuses rather than fall back to a remote one.

## Building a product on it

A product is a small package that imports `oaken_core`, names itself, and ships a command. See
[Oaken Mail](https://github.com/dfoulks1/oaken-mail) (`oknpost`) for a complete one, about 200 lines.

```python
import sys
from oaken_core import identity
from oaken_core.cli.commands import main

def run() -> int:
    identity.configure(app="my-app", title="My App", prog="myapp", daemon=[sys.executable, "-m", "myapp"])
    return main(prog="myapp")  # the child that `myapp start` spawns must call `configure` too: `python -m myapp` does
```

## Develop

```bash
uv sync
uv run pytest
```

The architecture decisions it inherited from oaken-mail are in [`docs/origin/`](docs/origin/). See [CONTRIBUTING.md](CONTRIBUTING.md)
(including the rules for AI-assisted contributions) and [SECURITY.md](SECURITY.md). Apache-2.0.
