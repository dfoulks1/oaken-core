# SPDX-License-Identifier: Apache-2.0
"""Plugins that live in a directory of plugin directories (``~/local/oaken-plugins``), not in a Python
environment. Each ``<root>/<plugin>/`` has its own ``pyproject.toml`` and a ``src/`` with the package; its
``[project.entry-points."oaken_core.<kind>s"]`` tables say what it offers, the way an installed plugin's do.

Nothing here installs or imports anything: ``prepare`` puts each ``src/`` on ``sys.path`` and ``entry_points``
reads the tomls, so the host loads them like any plugin. A plugin's own dependencies are the environment's
business: one that is missing shows as a load failure, with the reason, and the rest keep running.
"""

import importlib.util
import stat
import sys
import tomllib
from collections.abc import Iterable, Sequence
from importlib.metadata import EntryPoint
from pathlib import Path

from oaken_core.logging import get_logger

log = get_logger(__name__)
PACKAGE = "oaken_plugins"  # the one distribution of plugins; see ``plugins.catalog``
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
    """Make each plugin's package importable. Idempotent. A root that is itself a checkout of ``oaken-plugins`` (it holds
    ``src/oaken_plugins``) puts that ``src`` on the path, so naming the repo in ``[extensions] dirs`` is enough."""
    for root in roots:
        base = Path(root).expanduser()
        src = str(base / "src")
        if (base / "src" / PACKAGE).is_dir() and not base.stat().st_mode & stat.S_IWOTH and src not in sys.path:
            sys.path.append(src)
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


def origin(loadpath: str) -> str:
    """Where a plugin comes from, for a person: ``core`` (this package), ``plugins`` (installed from a package index) or ``local``
    (code on this disk: an extension directory, a checkout, a class path). ``loadpath`` is the entry point's ``module:attr``."""
    top = loadpath.partition(":")[0].partition(".")[0]
    if not top or top == "oaken_core":
        return "core"
    spec = importlib.util.find_spec(top) if top not in sys.modules else sys.modules[top].__spec__
    where = spec and (spec.origin if spec.origin not in (None, "namespace") else (list(spec.submodule_search_locations or [""])[0]))
    if not where:
        return "local"
    parts = Path(where).resolve().parts
    return "plugins" if ("site-packages" in parts or "dist-packages" in parts) else "local"


def in_directory(roots: Sequence[str], dirname: str) -> list[EntryPoint]:
    """Everything the plugin directory called ``dirname`` offers, in any group."""
    return [ep for d in plugin_dirs(roots) if d.name == dirname for ep in _declared(d)]
