# SPDX-License-Identifier: Apache-2.0
"""Structured logging to stderr. Secrets are redacted and long values truncated, so mail
content cannot leak into logs by accident."""

import os
import re
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, TextIO

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from oaken_core import paths
from oaken_core.settings import LogSettings

REDACTED = "[REDACTED]"
MAX_FIELD = 200
_SENSITIVE = re.compile(r"token|secret|password|authorization", re.IGNORECASE)
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


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


def exc_frames(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Swap ``exc_info`` for the exception's type and its frames. The message is left out: it
    may quote mail."""
    exc_info = event_dict.pop("exc_info", None)
    if exc_info is True:
        exc_info = sys.exc_info()
    elif isinstance(exc_info, BaseException):
        exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
    if exc_info and exc_info[1] is not None:
        event_dict["exception"] = type(exc_info[1]).__name__
        tb = traceback.extract_tb(exc_info[2])
        event_dict["frames"] = [f"{f.filename}:{f.lineno} {f.name}" for f in tb]
    return event_dict


def scrub(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Redact sensitive keys and truncate long values, at any depth."""
    return {k: _clean(k, v) for k, v in event_dict.items()}


class _LogFile:
    """An append-only, owner-only (``0600``) log file that rotates by size. A failing file never
    costs the process its log: the error is dropped."""

    def __init__(self, path: Path, max_bytes: int, backups: int) -> None:
        self.path, self.max_bytes, self.backups = path, max_bytes, backups
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._open()

    def _open(self) -> TextIO:
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")

    def _rotate(self) -> None:
        self._fh.close()
        for i in range(self.backups, 0, -1):
            src = self.path if i == 1 else self.path.with_name(f"{self.path.name}.{i - 1}")
            if src.exists():
                src.replace(self.path.with_name(f"{self.path.name}.{i}"))
        if not self.backups:
            self.path.unlink(missing_ok=True)
        self._fh = self._open()

    def write(self, text: str) -> int:
        with self._lock:
            try:
                if self._fh.tell() + len(text) > self.max_bytes:
                    self._rotate()
                self._fh.write(text)
                self._fh.flush()
            except (OSError, ValueError):
                pass
        return len(text)

    def flush(self) -> None:  # every write is flushed already
        pass


class _Tolerant:
    """``stream`` that never raises: a detached core's stderr may be closed, and logging must not
    take the process down with it."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream

    def write(self, text: str) -> int:
        try:
            return self.stream.write(text)
        except (OSError, ValueError):
            return len(text)

    def flush(self) -> None:
        try:
            self.stream.flush()
        except (OSError, ValueError):
            pass


def _log_path(name: str) -> Path:
    p = Path(name).expanduser()
    return p if p.is_absolute() else paths.data_dir() / p


def configure_logging(settings: LogSettings, stream: TextIO | None = None) -> None:
    """Configure structlog. With ``settings.file`` set, every line also goes to that file as one
    JSON object whatever ``format`` is, so the whole file parses; ``format`` shapes ``stream``
    (stderr by default). Calling it again replaces the setup, so a reload can change any of it."""
    out: TextIO = stream or sys.stderr
    sink: list[Processor] = []
    if settings.file:
        file = _LogFile(_log_path(settings.file), settings.file_max_bytes, settings.file_backups)
        as_json = structlog.processors.JSONRenderer()

        def to_file(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
            line = as_json(_, __, event_dict)
            file.write((line.decode() if isinstance(line, bytes) else line) + "\n")
            return event_dict

        sink = [to_file]
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if settings.format == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            exc_frames,
            scrub,
            *sink,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_LEVELS[settings.level]),
        logger_factory=structlog.PrintLoggerFactory(file=_Tolerant(out)),  # type: ignore[arg-type]
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
