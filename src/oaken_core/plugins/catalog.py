# SPDX-License-Identifier: Apache-2.0
"""The ``oaken_plugins`` catalog: the plugins that ship in the one ``oaken-plugins`` distribution.

The package has no top-level imports. Each plugin is a subpackage named ``<kind>_<name>`` (``formatter_ini``,
``provider_maildir``) and is imported only when someone adds it, so installing ``oaken-plugins`` loads nothing and
enables nothing: ``oaken plugin add ini`` does that, and ``oaken plugins`` lists what there is.

Listing imports nothing either: names come from the directory, summaries from each module's docstring (read, not run).
"""

import ast
import importlib
import importlib.util
import inspect
import pkgutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PACKAGE = "oaken_plugins"
DIST = "oaken-plugins"


@dataclass(frozen=True)
class Entry:
    name: str  # what ``plugin add`` takes: ``ini``
    kind: str  # ``formatter``
    module: str  # ``oaken_plugins.formatter_ini``
    summary: str


def _summary(source: Path) -> str:
    try:
        doc = ast.get_docstring(ast.parse(source.read_text(encoding="utf-8")))
    except (OSError, SyntaxError, ValueError):
        return ""
    return (doc or "").strip().splitlines()[0] if doc else ""


def entries(kinds: Iterable[str]) -> list[Entry]:
    """Every plugin in ``oaken_plugins``, by kind then name; empty when it is not installed."""
    spec = importlib.util.find_spec(PACKAGE)
    if spec is None or not spec.submodule_search_locations:
        return []
    known = sorted(kinds, key=len, reverse=True)  # ``llm_backend`` before ``llm``
    found: list[Entry] = []
    for mod in pkgutil.iter_modules(spec.submodule_search_locations):
        kind = next((k for k in known if mod.name.startswith(k + "_")), None)
        if kind is None:
            continue
        root = Path(spec.submodule_search_locations[0])
        source = root / mod.name / "__init__.py"
        found.append(Entry(mod.name[len(kind) + 1:], kind, f"{PACKAGE}.{mod.name}", _summary(source if source.is_file() else root / f"{mod.name}.py")))
    return sorted(found, key=lambda e: (e.kind, e.name))


def installed() -> bool:
    return importlib.util.find_spec(PACKAGE) is not None


def class_path(entry: Entry) -> str:
    """``module:Class`` for the one plugin class of ``entry.kind`` the module defines. Imports the module."""
    module = importlib.import_module(entry.module)
    made = [c for _, c in inspect.getmembers(module, inspect.isclass) if getattr(c, "kind", "") == entry.kind and c.__module__.startswith(entry.module)]
    if len(made) != 1:
        raise ValueError(f"{entry.module} should define exactly one {entry.kind} plugin class, found {len(made)}")
    return f"{entry.module}:{made[0].__name__}"


def hint(kinds: Iterable[str]) -> str:
    """What to tell someone whose plugin name matched nothing."""
    found = entries(kinds)
    if not installed():
        return f"`pip install {DIST}` has more; `oaken plugins` then lists them"
    return "in " + DIST + ": " + ", ".join(f"{e.name}" for e in found) + " (`oaken plugins` lists them)" if found else f"{DIST} has no plugin for it"
