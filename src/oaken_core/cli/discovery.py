# SPDX-License-Identifier: Apache-2.0
"""Finding the running core: the runtime file, the API token, and the dev overrides.

The daemon writes a **runtime file** when it starts and removes it on a clean exit. The
CLI reads it to learn where core listens and which token to present. This module owns that
contract from the client side.

The runtime file
-----------------
Path: ``paths.runtime_file("runtime.json")`` — the platform runtime directory
(``$XDG_RUNTIME_DIR/oaken`` on Linux), or ``$OAKEN_HOME/run/runtime.json`` when ``OAKEN_HOME`` is
set. Mode ``0600``. JSON, UTF-8:

.. code-block:: json

    {
      "pid": 12345,
      "host": "127.0.0.1",
      "port": 8123,
      "version": "0.2.0",
      "started_at": "2026-10-20T19:57:04.481Z"
    }

``host`` is loopback unless the owner set ``remote_ok``. ``port`` is the concrete bound port
(``0`` while core is starting: it claims the file first, then publishes the port). A runtime
file whose ``pid`` names no live process is **stale** (core crashed or was killed) and is
treated as "core is not running".

The token
---------
Path: ``paths.config_dir() / "api_token"`` — ``~/.config/oaken/api_token``, mode ``0600``, a single
line. The CLI sends it as ``Authorization: Bearer <token>``. A missing token file, with a live
runtime file, means core is mid-start or the file was removed by hand; the CLI reports core-down
rather than guessing.

Dev overrides
-------------
``--url http://127.0.0.1:PORT`` and ``--token-file PATH`` (or ``OAKEN_URL`` / ``OAKEN_TOKEN_FILE``)
bypass the runtime file for development against a hand-started server. A non-loopback ``--url`` is
refused unless ``--remote`` is given.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from oaken_core import paths
from oaken_core.cli.exits import CliError, CoreUnavailable, ExitCode

RUNTIME_FILE = "runtime.json"
TOKEN_FILE = "api_token"  # noqa: S105  — a filename, not a secret
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


@dataclass(frozen=True)
class Endpoint:
    """How to reach core: the base URL and the bearer token. Produced by :func:`discover`."""

    base_url: str
    token: str

    @property
    def is_loopback(self) -> bool:
        return (urlparse(self.base_url).hostname or "") in _LOOPBACK


def _pid_alive(pid: int) -> bool:
    """Is a process with this pid running? ``os.kill(pid, 0)`` is the portable probe on POSIX; on
    Windows it raises ``OSError`` for a dead pid too, so the same branch holds."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, just not ours to signal
    except OSError:
        return False
    return True


def _read_runtime(path: Path) -> dict[str, object]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise CoreUnavailable("no runtime file") from None
    except OSError as exc:
        raise CoreUnavailable(f"runtime file unreadable: {exc}") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CoreUnavailable(f"runtime file is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise CoreUnavailable("runtime file is not a JSON object")
    return data


def _read_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise CoreUnavailable("no API token file") from None
    except OSError as exc:
        raise CoreUnavailable(f"token file unreadable: {exc}") from None
    if not token:
        raise CoreUnavailable("API token file is empty")
    return token


def _require_loopback(url: str, allow_remote: bool) -> None:
    """A non-loopback url needs ``allow_remote`` (``--remote``): past loopback the token is the
    only gate, so the person says so on purpose. Without it the CLI never sends the token away."""
    host = urlparse(url).hostname or ""
    if host not in _LOOPBACK and not allow_remote:
        raise CliError(
            f"refusing a non-loopback API url ({host!r}); pass --remote to use one", ExitCode.USAGE
        )


def discover(
    *,
    url: str | None = None,
    token_file: str | None = None,
    runtime_path: Path | None = None,
    token_path: Path | None = None,
    token: str | None = None,
    allow_remote: bool = False,
) -> Endpoint:
    """Resolve how to reach core.

    Precedence: explicit ``url``/``token_file`` arguments (the CLI flags), then the ``OAKEN_URL`` /
    ``OAKEN_TOKEN_FILE`` environment, then the runtime file. A ``url`` override still needs a
    token, taken from ``token_file`` if given, else the standard token file.

    Raises :class:`CoreUnavailable` (exit 3) when the runtime file is absent, stale, or the token
    is missing; :class:`CliError` (exit 2) for a non-loopback override url.
    """
    url = url or os.environ.get("OAKEN_URL") or None
    token_file = token_file or os.environ.get("OAKEN_TOKEN_FILE") or None
    token_src = token_path or (Path(token_file).expanduser() if token_file else _token_path())

    def credential() -> str:
        return token if token is not None else _read_token(token_src)

    if url:
        _require_loopback(url, allow_remote)
        return Endpoint(base_url=url.rstrip("/"), token=credential())

    try:
        runtime = _read_runtime(runtime_path or _runtime_path())
        pid = runtime.get("pid")
        if not isinstance(pid, int) or not _pid_alive(pid):
            raise CoreUnavailable("runtime file is stale (process not running)")
        host = runtime.get("host", "127.0.0.1")
        port = runtime.get("port")
        if port == 0:
            raise CoreUnavailable("core is still starting (no port yet)")
        if host not in _LOOPBACK or not isinstance(port, int) or not (0 < port <= 65535):
            raise CoreUnavailable("runtime file does not name a loopback host and port")
    except CoreUnavailable:
        raise
    return Endpoint(base_url=f"http://{host}:{port}", token=credential())


def runtime_info(runtime_path: Path | None = None) -> dict[str, object] | None:
    """The runtime file's contents if the process it names is alive, else ``None`` (absent, stale
    or unreadable). Unlike :func:`discover` this does not need the port or the token: it answers
    "is a core running?" for ``oaken status``, including a core that is still starting."""
    try:
        data = _read_runtime(runtime_path or _runtime_path())
    except CoreUnavailable:
        return None
    pid = data.get("pid")
    if not isinstance(pid, int) or not _pid_alive(pid):
        return None
    return data


def _runtime_path() -> Path:
    return paths.runtime_file(RUNTIME_FILE)


def _token_path() -> Path:
    return paths.config_dir() / TOKEN_FILE
