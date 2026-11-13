# SPDX-License-Identifier: Apache-2.0
"""Logging on Python's own ``logging``: five levels (DEBUG, INFO, WARNING, ERROR, CRITICAL), a
level for the whole package and a level per logger, to stderr and optionally a file.

``get_logger(name)`` returns a logger that takes fields as keywords
(``log.warning("job failed", job_id=j)``). Secrets are redacted and long values truncated before
they are written, and an exception is logged as its type and frames, never its message (which may
quote private text). Core configures only the ``oaken_core`` logger, never the root, so a host
application's logging is left alone.
"""

import contextlib
import contextvars
import json
import logging
import logging.handlers
import os
import re
import sys
import traceback
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from oaken_core import paths
from oaken_core.settings import LogSettings

ROOT = "oaken_core"
REDACTED = "[REDACTED]"
MAX_FIELD = 200
_SENSITIVE = re.compile(r"token|secret|password|authorization", re.IGNORECASE)
_CTX: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("oaken_log_ctx", default={})  # noqa: B039 - never mutated: bind() sets a new dict
_overridden: set[str] = set()  # loggers whose level a previous configure_logging set


def _clean(key: str, value: Any) -> Any:
    if _SENSITIVE.search(key):
        return REDACTED
    if isinstance(value, str):
        if len(value) > MAX_FIELD:
            return f"{value[:MAX_FIELD]}...[truncated {len(value) - MAX_FIELD} chars]"
        return value
    if isinstance(value, dict):
        return {k: _clean(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(key, v) for v in value]
    return value


@contextlib.contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Fields added to every log line written inside the block (a job id, for example)."""
    token = _CTX.set({**_CTX.get(), **fields})
    try:
        yield
    finally:
        _CTX.reset(token)


class Log(logging.LoggerAdapter):  # type: ignore[type-arg]
    """A logger that takes fields as keyword arguments."""

    _STDLIB = ("exc_info", "stack_info", "stacklevel", "extra")

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        fields = {k: kwargs.pop(k) for k in list(kwargs) if k not in self._STDLIB}
        kwargs["extra"] = {"fields": fields}
        kwargs.setdefault("stacklevel", 1)
        return msg, kwargs


def get_logger(name: str | None = None) -> Log:
    return Log(logging.getLogger(name or ROOT), {})


def _event(record: logging.LogRecord) -> dict[str, Any]:
    """One record as a flat, scrubbed dict."""
    out: dict[str, Any] = {
        "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
        "level": record.levelname.lower(),
        "logger": record.name,
        "event": record.getMessage(),
    }
    fields = {**_CTX.get(), **getattr(record, "fields", {})}
    out.update({k: _clean(k, v) for k, v in fields.items()})
    if record.exc_info and record.exc_info[1] is not None:
        out["exception"] = type(record.exc_info[1]).__name__
        frames = traceback.extract_tb(record.exc_info[2])
        out["frames"] = [f"{f.filename}:{f.lineno} {f.name}" for f in frames]
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(_event(record), default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        e = _event(record)
        head = f"{e.pop('ts')} {e.pop('level').upper():8} {e.pop('logger')}  {e.pop('event')}"
        return head + "".join(f" {k}={v}" for k, v in e.items())


class _Quiet(logging.Handler):
    """Mixin behaviour: a failing sink (a closed stderr, a full disk) never costs the process."""

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        pass


class _StreamHandler(_Quiet, logging.StreamHandler):  # type: ignore[type-arg]
    pass


class _FileHandler(_Quiet, logging.handlers.RotatingFileHandler):
    """An owner-only (``0600``) log file that rotates by size."""

    def _open(self) -> TextIO:
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")


def _log_path(name: str) -> Path:
    p = Path(name).expanduser()
    return p if p.is_absolute() else paths.data_dir() / p


def configure_logging(settings: LogSettings, stream: TextIO | None = None) -> None:
    """Configure the ``oaken_core`` logger. With ``settings.file`` set, every line also goes to
    that file as JSON whatever ``format`` is, so the whole file parses; ``format`` shapes the
    stream (stderr by default). Calling it again replaces the setup, so a reload can change any
    of it, including dropping a per-logger level it set before."""
    root = logging.getLogger(ROOT)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for name in _overridden:
        logging.getLogger(name).setLevel(logging.NOTSET)
    _overridden.clear()

    console = _StreamHandler(stream or sys.stderr)
    console.setFormatter(JsonFormatter() if settings.format == "json" else ConsoleFormatter())
    root.addHandler(console)
    if settings.file and settings.enabled:
        path = _log_path(settings.file)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = _FileHandler(path, maxBytes=settings.file_max_bytes, backupCount=settings.file_backups)
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
    root.setLevel(settings.level)
    root.propagate = False  # we own this subtree; the host's root logger is left alone
    for name, level in settings.levels.items():
        logging.getLogger(name).setLevel(level)
        _overridden.add(name)
