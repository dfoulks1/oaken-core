# SPDX-License-Identifier: Apache-2.0
"""The two small files a client needs to find core: the runtime file and the API token.

Both are owner-only (``0600``) and written without a window where they are readable by anyone else.
The runtime file is also the single-instance lock: it is created exclusively, so two starts cannot
both win, and a file whose process is gone is taken over rather than trusted. On Windows POSIX mode
bits do not exist; the files live in the per-user profile (``%APPDATA%``), whose ACL already
restricts them to the user, and ``chmod`` is attempted as a best effort.
"""

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from oaken_core.clock import Clock
from oaken_core.errors import ConfigError, Conflict

TOKEN_FILE = "api_token"  # noqa: S105 (a filename, not a secret)
MCP_TOKEN_FILE = "mcp_token"  # noqa: S105 (a filename, not a secret)
RUNTIME_FILE = "runtime.json"


def _private_write(path: Path, text: str, *, exclusive: bool) -> None:
    """Write ``text`` to ``path`` created as ``0600`` from the first byte."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    except BaseException:
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    with contextlib.suppress(OSError, NotImplementedError):
        path.chmod(0o600)


def load_or_create_token(path: Path) -> str:
    """The API token, created on first start (256 random bits). A token file anyone else can read
    is tightened to ``0600`` and used: refusing to start would help nobody."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = ""
    except OSError as exc:
        raise ConfigError(f"cannot read the API token file {path}: {exc}") from exc
    if token:
        if sys.platform != "win32" and path.stat().st_mode & 0o077:
            path.chmod(0o600)
        return token
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    _private_write(path, token + "\n", exclusive=False)
    return token


def same_secret(supplied: str, expected: str) -> bool:
    """Constant-time comparison. Both sides are hashed first so the length of the real token does
    not leak through a short-circuit either."""
    return hmac.compare_digest(
        hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(expected.encode()).digest()
    )


def pid_alive(pid: int) -> bool:
    """Is a process with this id running? Signal 0 on POSIX, ``OpenProcess`` on Windows."""
    if pid <= 0:
        return False
    if sys.platform == "win32":  # pragma: no cover (Windows only)
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, not ours to signal
    except OSError:
        return False
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    """A process that exited but whose parent has not collected it still answers signal 0."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:  # no /proc (macOS, BSD): treat as alive
        return False
    return stat.rpartition(")")[2].strip()[:1] == "Z"


def read_runtime(path: Path) -> dict[str, Any] | None:
    """The runtime file's contents, or ``None`` if it is missing or not ours to understand."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class RuntimeFile:
    """``runtime.json``: pid, host, port, version and start time, while core runs."""

    def __init__(self, path: Path, clock: Clock, version: str) -> None:
        self.path, self.clock, self.version = path, clock, version
        self.pid = os.getpid()
        self._held = False
        self._started: datetime = clock.now()

    def _payload(self, port: int, console_port: int = 0) -> str:
        data: dict[str, Any] = {
            "pid": self.pid,
            "host": "127.0.0.1",
            "port": port,
            "console_port": console_port,
            "version": self.version,
            "started_at": self._started.strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{self._started.microsecond // 1000:03d}Z",
        }
        return json.dumps(data) + "\n"

    def claim(self) -> None:
        """Become the one running core, or raise ``Conflict`` if another is. The file is made
        exclusively with port 0 ("starting"); ``publish`` fills in the real port. A file whose
        process is gone is replaced."""
        self._started = self.clock.now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                _private_write(self.path, self._payload(0), exclusive=True)
            except FileExistsError:
                other = self.read()
                pid = other.get("pid") if other else None
                if isinstance(pid, int) and pid != self.pid and pid_alive(pid):
                    raise Conflict(f"core is already running (pid {pid})") from None
                with contextlib.suppress(FileNotFoundError):
                    self.path.unlink()  # stale or unreadable: ours to replace
                continue
            self._held = True
            return
        raise Conflict("another core is starting; try again")

    def publish(self, port: int, console_port: int = 0) -> None:
        """Say where core listens. Written to a sibling and renamed, so a reader sees the old file
        or the new one, never half of it."""
        tmp = self.path.with_name(self.path.name + f".{self.pid}.tmp")
        _private_write(tmp, self._payload(port, console_port), exclusive=False)
        os.replace(tmp, self.path)

    def read(self) -> dict[str, Any] | None:
        return read_runtime(self.path)

    def release(self) -> None:
        """Remove the file, but only if it is ours."""
        if not self._held:
            return
        self._held = False
        other = self.read()
        if other is None or other.get("pid") == self.pid:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
