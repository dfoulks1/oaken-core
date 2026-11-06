# SPDX-License-Identifier: Apache-2.0
"""Plugins that live in a directory of plugin directories (``~/local/oaken-plugins``), not in a Python
environment. Each ``<root>/<plugin>/`` has its own ``pyproject.toml`` and a ``src/`` with the package; its
``[project.entry-points."oaken_core.<kind>s"]`` tables say what it offers, the way an installed plugin's do.

Nothing here installs or imports anything: ``prepare`` puts each ``src/`` on ``sys.path`` and ``entry_points``
reads the tomls, so the host loads them like any plugin. A plugin's own dependencies are the environment's
business: one that is missing shows as a load failure, with the reason, and the rest keep running.
"""

import stat
import sys
import tomllib
from collections.abc import Iterable, Sequence
from importlib.metadata import EntryPoint
from pathlib import Path

from oaken_core.logging import get_logger

log = get_logger(__name__)
GROUP_PREFIX = "oaken_core."  # a plugin made for another Oaken (``oaken.providers``) is not ours to load


def plugin_dirs(roots: Sequence[str]) -> list[Path]:
    """The plugin directories under each root: a directory with a ``pyproject.toml``. A root that is missing, or that
    anyone may write to (a stranger's file would run as the daemon), is skipped and logged."""
    found: list[Path] = []
    for root in roots:
        base = Path(root).expanduser()
        if not base.is_dir():
            log.warning("extensions directory is missing", path=str(base))
            continue
        if base.stat().st_mode & stat.S_IWOTH:
            log.error("extensions directory is world-writable: skipped", path=str(base))
            continue
        found += [d for d in sorted(base.iterdir()) if d.is_dir() and (d / "pyproject.toml").is_file()]
    return found


def prepare(roots: Sequence[str]) -> None:
    """Make each plugin's package importable. Idempotent."""
    for d in plugin_dirs(roots):
        src = str(d / "src")
        if (d / "src").is_dir() and src not in sys.path:
            sys.path.append(src)


def _declared(d: Path) -> Iterable[EntryPoint]:
    try:
        data = tomllib.loads((d / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.error("plugin directory has an unreadable pyproject.toml", path=str(d), error=str(exc))
        return
    for group, table in data.get("project", {}).get("entry-points", {}).items():
        if group.startswith(GROUP_PREFIX) and isinstance(table, dict):
            for name, value in table.items():
                yield EntryPoint(name=str(name), value=str(value), group=group)


def entry_points(roots: Sequence[str], group: str) -> list[EntryPoint]:
    """What the plugin directories offer in ``group``."""
    return [ep for d in plugin_dirs(roots) for ep in _declared(d) if ep.group == group]


def in_directory(roots: Sequence[str], dirname: str) -> list[EntryPoint]:
    """Everything the plugin directory called ``dirname`` offers, in any group."""
    return [ep for d in plugin_dirs(roots) if d.name == dirname for ep in _declared(d)]
