# SPDX-License-Identifier: Apache-2.0
"""Check a plugin's ``ui()`` and place rows in an orderable list ."""

import re
from collections.abc import Mapping, Sequence

from oaken_core.errors import InvalidInput
from oaken_core.plugins.contracts import UiPage, UiSpec

_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")
_OP = re.compile(r"^[a-z][a-z0-9_]{0,60}$")


def check_ui(spec: object) -> list[str]:
    """Problems with a ``UiSpec``, in plain words; empty when it is sound. Core serves a spec only
    when this is empty, and otherwise falls back to the schema-built modal."""
    if spec is None:
        return []
    if not isinstance(spec, UiSpec):
        return ["ui() must return a UiSpec or None"]
    problems: list[str] = []
    seen: set[str] = set()
    for page in spec.pages:
        if not isinstance(page, UiPage) or not _NAME.match(page.name):
            problems.append(
                f"a page needs a plain lowercase name, got {getattr(page, 'name', page)!r}"
            )
            continue
        if page.name in seen:
            problems.append(f"two pages are named {page.name}")
        seen.add(page.name)
        for role, op in (("load", page.load), ("submit", page.submit), ("test", page.test),
                         ("order", page.order)):  # fmt: skip
            if (role == "load" or op) and not _OP.match(op or ""):
                problems.append(
                    f"page {page.name}: {role} must name one of the plugin's operations"
                )
        names = [f.name for f in page.fields]
        if len(set(names)) != len(names):
            problems.append(f"page {page.name}: two fields share a name")
        for f in page.fields:
            if f.kind == "enum" and not f.choices:
                problems.append(f"page {page.name}: enum field {f.name} has no choices")
    for b in spec.buttons:
        if not _OP.match(b.operation or ""):
            problems.append(f"button {b.label!r} must name one of the plugin's operations")
    return problems


def reposition(ids: Sequence[str], asked: Mapping[str, object]) -> list[str]:
    """The new order of ``ids`` once each row in ``asked`` has been given the place typed for it
    (1 is first). A row whose number did not change is not moved. A moved row lands exactly where it
    asked; the rows that did not move fill the other places in their old order. Two moved rows asking
    for one place: the earlier in the list keeps it, the other takes the next free place after it
    (or the last free one before it). Raises ``InvalidInput``, changing nothing, if an entry is not
    a whole number from 1 to n."""
    n = len(ids)
    wanted: dict[str, int] = {}
    for i, rid in enumerate(ids, 1):
        raw = asked.get(rid)
        if raw is None or str(raw).strip() == "":
            continue
        text = str(raw).strip()
        if not re.fullmatch(r"\d+", text) or not 1 <= int(text) <= n:
            raise InvalidInput(f"{rid}: the position must be a whole number from 1 to {n}")
        if int(text) != i:
            wanted[rid] = int(text)
    slots: dict[int, str] = {}
    for rid in ids:  # earlier rows win a collision
        if rid not in wanted:
            continue
        place = wanted[rid]
        free = [p for p in range(1, n + 1) if p not in slots]
        slots[next((p for p in free if p >= place), free[-1])] = rid
    rest = iter(r for r in ids if r not in wanted)
    return [slots[p] if p in slots else next(rest) for p in range(1, n + 1)]
