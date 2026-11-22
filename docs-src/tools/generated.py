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

from pathlib import Path

from model import BuildError, Config, Page, page_from_meta

Builder = Callable[[Config], Page]

# Modules imported by load_all(); each calls register() for the pages it owns.
GENERATOR_MODULES: list[str] = [
    "softmap",
]

ADR_DIRS = ("decisions",)  # where the decision records live under docs-src/
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


def _adr_row(path: "Path", cfg: Config) -> tuple[str, str, str, str, str]:
    """(number, title, status, date, repo path) of one ADR: the older ``- Status:`` bullets, or MADR's front matter."""
    text = path.read_text(encoding="utf-8")
    title = re.search(r"^# (?:\d+\.\s*)?(.+)$", text, re.MULTILINE)
    if text.startswith("---\n"):  # MADR: status and date in a YAML header
        head = text.split("\n---", 1)[0]
        status = re.search(r"^status:\s*(.+)$", head, re.MULTILINE)
        date = re.search(r"^date:\s*(.+)$", head, re.MULTILINE)
    else:
        status = re.search(r"^[-*]\s*Status:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
        date = re.search(r"^[-*]\s*Date:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
    if not (title and status and date):
        raise BuildError(f"{path}: an ADR needs a '# Title' heading and a status and a date (bullets, or MADR front matter)")
    return (path.name[:4], title.group(1).strip(), status.group(1).strip(), date.group(1).strip(), path.relative_to(cfg.repo_root).as_posix())


def _adr_index(cfg: Config) -> Page:
    """Status, title and date of every ADR in docs-src/<ADR_DIRS>/NNNN-*.md, in number order."""
    paths = [p for d in ADR_DIRS for p in (cfg.docs_dir / d).glob("[0-9][0-9][0-9][0-9]-*.md")]
    rows = [_adr_row(p, cfg) for p in sorted(paths, key=lambda p: p.name)]
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
