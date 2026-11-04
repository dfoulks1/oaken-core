# SPDX-License-Identifier: Apache-2.0
"""Printing results: machine JSON, or human text that is safe on a terminal.

Two modes, chosen by ``--json``:

* **JSON** (``--json``, or stdout is not a TTY and the command has structured output): the original
  data, unchanged. A script must get the bytes as core produced them, so **nothing is sanitized
  here** — a consumer of JSON is not a terminal .
* **Human**: text for a person at a terminal. Every string that came from outside is passed through
  :func:`oaken_core.cli.sanitize.sanitize` first, because a terminal will act on control/escape
  sequences. Colour and tables are used only when stdout is a TTY.

``rich`` is imported lazily and only for the human path, so it never costs anything on ``--json``
or on ``--help`` (the <300 ms startup budget). If ``rich`` is not installed, the human path falls
back to plain text — the CLI never hard-depends on it.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

from oaken_core.cli.sanitize import sanitize, sanitize_line


def is_tty(stream: TextIO | None = None) -> bool:
    stream = stream or sys.stdout
    try:
        return stream.isatty()
    except (ValueError, AttributeError):
        return False


def print_json(data: Any, *, stream: TextIO | None = None) -> None:
    """Write ``data`` as pretty JSON. Never sanitized — this is the machine contract."""
    stream = stream or sys.stdout
    json.dump(data, stream, indent=2, ensure_ascii=False, default=str, sort_keys=False)
    stream.write("\n")


def print_text(text: str, *, stream: TextIO | None = None) -> None:
    """Write human text, sanitized for the terminal."""
    stream = stream or sys.stdout
    stream.write(sanitize(text))
    stream.write("\n")


def print_table(
    rows: Sequence[Mapping[str, Any]],
    columns: Sequence[str],
    *,
    title: str | None = None,
    stream: TextIO | None = None,
    footer_rows: int = 0,
) -> None:
    """Render a table of ``rows`` restricted to ``columns``. Cell values are sanitized to one line.
    The last ``footer_rows`` rows (a totals row) are set off from the rest by a rule.

    Uses ``rich`` when it is installed and stdout is a TTY; otherwise a plain aligned text table.
    """
    stream = stream or sys.stdout
    str_rows = [[sanitize_line(str(row.get(col, ""))) for col in columns] for row in rows]
    if is_tty(stream):
        try:
            _rich_table(columns, str_rows, title, stream, footer_rows)
            return
        except ImportError:
            pass
    _plain_table(columns, str_rows, title, stream, footer_rows)


def _rich_table(
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    title: str | None,
    stream: TextIO,
    footer_rows: int = 0,
) -> None:
    from rich.console import Console
    from rich.table import Table

    table = Table(title=title)
    for col in columns:
        table.add_column(col)
    for i, row in enumerate(rows):
        if footer_rows and i == len(rows) - footer_rows:
            table.add_section()
        table.add_row(*row)
    Console(file=stream).print(table)


def _plain_table(
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    title: str | None,
    stream: TextIO,
    footer_rows: int = 0,
) -> None:
    widths = [len(c) for c in columns]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    if title:
        stream.write(f"{title}\n")
    header = "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
    stream.write(header + "\n")
    rule = "  ".join("-" * widths[i] for i in range(len(columns))) + "\n"
    stream.write(rule)
    for n, row in enumerate(rows):
        if footer_rows and n == len(rows) - footer_rows:
            stream.write(rule)
        stream.write("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) + "\n")
