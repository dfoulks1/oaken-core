# SPDX-License-Identifier: Apache-2.0
"""A plugin's own text (its docstring, its help) as HTML: paragraphs reflowed, an indented block kept as code, `a span` as code.

Plain text in, escaped HTML out: nothing a plugin writes is ever markup.
"""

import re

from markupsafe import Markup, escape

_SPAN = re.compile(r"`([^`\n]+)`")


def _inline(text: str) -> str:
    return _SPAN.sub(r"<code>\1</code>", str(escape(text)))


def docs_html(text: str) -> Markup:
    """Blank-line separated blocks: a block whose lines are all indented is code, any other is a paragraph with its lines joined."""
    out: list[str] = []
    for block in re.split(r"\n\s*\n", (text or "").strip("\n")):
        lines = block.splitlines()
        if not lines:
            continue
        if all(line.startswith((" ", "\t")) for line in lines):
            margin = min(len(line) - len(line.lstrip()) for line in lines)
            out.append('<pre class="mono">' + "\n".join(str(escape(line[margin:])) for line in lines) + "</pre>")
        else:
            out.append("<p>" + _inline(" ".join(line.strip() for line in lines)) + "</p>")
    return Markup("\n".join(out))
