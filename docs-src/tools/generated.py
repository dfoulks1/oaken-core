# SPDX-License-Identifier: Apache-2.0
"""Generated pages: a module registers (page_id, builder) and the site registry lists the id.

A builder takes a Config and returns a Page, exactly as a Markdown file would. Later prompts
add their own generators (plugin contracts, operations, CLI and API references) by calling
``register`` at import time and by adding the module name to GENERATOR_MODULES. Do not
register a generator, or list its page in site.toml, before the thing it documents exists:
empty reference pages are not wanted.
"""

import importlib
import re
from collections.abc import Callable

from model import BuildError, Config, Page, page_from_meta

Builder = Callable[[Config], Page]

# Modules imported by load_all(); each calls register() for the pages it owns.
GENERATOR_MODULES: list[str] = [
    "softmap",
]

ADR_DIR = "origin"  # where the decision records live under docs/
_REGISTRY: dict[str, Builder] = {}


def register(page_id: str, builder: Builder) -> None:
    if page_id in _REGISTRY:
        raise BuildError(f"generated page {page_id!r} is registered twice")
    _REGISTRY[page_id] = builder


def get(page_id: str) -> Builder:
    try:
        return _REGISTRY[page_id]
    except KeyError:
        raise BuildError(
            f"site.toml lists generated page {page_id!r} but no generator exists"
        ) from None


def load_all() -> None:
    for name in GENERATOR_MODULES:
        importlib.import_module(name)


def _adr_index(cfg: Config) -> Page:
    """Status, title and date of every ADR in docs/<ADR_DIR>/NNNN-*.md."""
    rows: list[tuple[str, str, str, str, str]] = []
    adr_dir = cfg.docs_dir / ADR_DIR
    for path in sorted(adr_dir.glob("[0-9][0-9][0-9][0-9]-*.md")):
        text = path.read_text(encoding="utf-8")
        t = re.search(r"^# (?:\d+\.\s*)?(.+)$", text, re.MULTILINE)
        s = re.search(r"^[-*]\s*Status:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
        d = re.search(r"^[-*]\s*Date:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
        if not (t and s and d):
            raise BuildError(
                f"{path}: an ADR needs a '# N. Title' heading and Status and Date bullets"
            )
        rel = path.relative_to(cfg.repo_root).as_posix()
        rows.append(
            (path.name[:4], t.group(1).strip(), s.group(1).strip(), d.group(1).strip(), rel)
        )
    lines = [
        "## Decisions",
        "",
        "An architecture decision record (ADR) captures one significant decision, why it was",
        "made, and what follows from it. Records are never rewritten; a later record supersedes",
        "an earlier one.",
        "",
        "| # | Decision | Status | Date |",
        "|---|---|---|---|",
    ]
    for num, title, status, date, rel in rows:
        lines.append(f"| {num} | [{title}](repo:{rel}) | {status} | {date} |")
    if not rows:
        lines.append("| | No decisions recorded yet | | |")
    return page_from_meta(
        "adr/index",
        "Architecture decisions",
        "\n".join(lines) + "\n",
        {
            "subtitle": "Architecture decisions",
            "lead": "The significant design decisions behind Oaken Core, in the order made.",
            "badges": [["text-bg-secondary", "Generated"]],
            "group": "project",
            "order": 20,
            "description": "Every architecture decision record, with its status and date.",
        },
        None,
        False,
    )


register("adr/index", _adr_index)
