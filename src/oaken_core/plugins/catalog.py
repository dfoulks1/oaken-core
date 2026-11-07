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
    api: str = ""  # the plugin API version it declares, if its source says one: blank means whatever core speaks
    requires: str = ""  # the core it says it needs (``requires_core``), if it says
    tags: tuple[str, ...] = ()  # what it says it is for (``tags``), if it says


def declared(path: Path) -> dict[str, str]:
    """``api_version``, ``requires_core`` and ``tags`` as a plugin's source writes them (a class body assigning a constant, or for
    ``tags`` a tuple of them, joined with commas), read with ``ast``: nothing is imported. ``path`` is a package folder or one module.
    A key the source never sets, or sets empty, is absent."""
    found: dict[str, str] = {}
    for f in [path] if path.is_file() else sorted(path.rglob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for st in cls.body:
                target = st.target if isinstance(st, ast.AnnAssign) else (st.targets[0] if isinstance(st, ast.Assign) and len(st.targets) == 1 else None)
                value = getattr(st, "value", None)
                if isinstance(target, ast.Name) and target.id in ("api_version", "requires_core") and isinstance(value, ast.Constant) and value.value not in ("", None):
                    found.setdefault(target.id, str(value.value))
                elif isinstance(target, ast.Name) and target.id == "tags" and isinstance(value, ast.Tuple | ast.List):
                    names = [e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str) and e.value]
                    if names:
                        found.setdefault("tags", ",".join(names))
    return found


def _summary(source: Path) -> str:
    try:
        doc = ast.get_docstring(ast.parse(source.read_text(encoding="utf-8")))
    except (OSError, SyntaxError, ValueError):
        return ""
    return (doc or "").strip().splitlines()[0].replace("``", "") if doc else ""


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
        where = root / mod.name if (root / mod.name).is_dir() else root / f"{mod.name}.py"
        decl = declared(where)
        found.append(Entry(mod.name[len(kind) + 1:].replace("_", "-"), kind, f"{PACKAGE}.{mod.name}", _summary(source if source.is_file() else root / f"{mod.name}.py"),
                           decl.get("api_version", ""), decl.get("requires_core", ""), tuple(decl["tags"].split(",")) if decl.get("tags") else ()))  # fmt: skip
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
