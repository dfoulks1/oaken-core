# SPDX-License-Identifier: Apache-2.0
"""Where Oaken keeps its files. ``OAKEN_HOME`` relocates everything under one root."""

import contextlib
import os
from pathlib import Path

import platformdirs

APP = "oaken"
HOME_ENV = "OAKEN_HOME"


POINTER_ENV = "OAKEN_STATE_DIR"  # where the last-known-home pointer lives (tests redirect it)
POINTER_FILE = "last_home"


def _pointer_path() -> Path:
    base = os.environ.get(POINTER_ENV)
    return (
        Path(base).expanduser() if base else Path(platformdirs.user_state_dir(APP))
    ) / POINTER_FILE


def _remembered() -> Path | None:
    """The home core last ran under, if that folder still exists. Kept outside the home itself
    because the databases that could say it live inside it."""
    try:
        raw = _pointer_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    path = Path(raw).expanduser() if raw else None
    return path.resolve() if path is not None and path.is_dir() else None


def remember_home() -> bool:
    """Record the current home as the last known good one. ``True`` if something was written.
    Only done when ``OAKEN_HOME`` is set explicitly: the platform default needs no pointer."""
    home = _home()
    if home is None or os.environ.get(HOME_ENV) is None:
        return False
    target = _pointer_path()
    if _remembered() == home:
        return False
    try:
        _ensure(target.parent)
        target.write_text(str(home) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def home_source() -> tuple[str, Path | None]:
    """How the home was chosen: ``env`` (``OAKEN_HOME``), ``remembered`` (the last known good
    home, used when the variable is unset) or ``default`` (the platform folders)."""
    raw = os.environ.get(HOME_ENV)
    if raw:
        return "env", Path(raw).expanduser().resolve()
    remembered = _remembered()
    if remembered is not None:
        return "remembered", remembered
    return "default", None


def _home() -> Path | None:
    return home_source()[1]


def _ensure(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError, NotImplementedError):  # e.g. Windows
        path.chmod(0o700)
    return path


def data_dir() -> Path:
    """Databases and other durable state."""
    home = _home()
    return _ensure(home / "data" if home else Path(platformdirs.user_data_dir(APP)))


def plugins_dir() -> Path:
    """Folders plugins own, ``<kind>/<name>/<folder>`` beneath it (ADR 0016)."""
    home = _home()
    return _ensure(home / "plugins" if home else Path(platformdirs.user_data_dir(APP)) / "plugins")


def config_dir() -> Path:
    """``config.toml`` lives here."""
    home = _home()
    return _ensure(home / "config" if home else Path(platformdirs.user_config_dir(APP)))


def runtime_file(name: str = "runtime.json") -> Path:
    """A file for the running core (port and token). Its directory is created, the file is not."""
    home = _home()
    base = home / "run" if home else Path(platformdirs.user_runtime_dir(APP, ensure_exists=False))
    return _ensure(base) / name
