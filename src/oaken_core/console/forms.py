# SPDX-License-Identifier: Apache-2.0
"""Fields for the console's forms, and reading them back.

A field is either a ``UiField`` a plugin declared or one built from a JSON Schema property (the
fallback modal). Both become ``Field`` so one template draws them and one parser reads them.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from oaken_core.errors import InvalidInput
from oaken_core.plugins.contracts import UiField

KINDS = ("text", "number", "bool", "enum", "list", "json")


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str = "text"  # one of KINDS; ``json`` is a property the form cannot draw better
    choices: tuple[str, ...] = ()
    help: str = ""
    required: bool = False
    integer: bool = False
    default: Any = None  # what applies when the box is left empty; shown as the placeholder


def from_ui(f: UiField) -> Field:
    return Field(f.name, f.label or f.name, f.kind, f.choices, f.help, f.required)


def from_schema(schema: Mapping[str, Any]) -> list[Field]:
    """One field per property of a pydantic JSON Schema. A property that is not a plain scalar, an
    enum or a list of strings is a JSON box."""
    required = set(schema.get("required", ()))
    out: list[Field] = []
    for name, prop in (schema.get("properties") or {}).items():
        kind, choices, integer = "json", (), False
        types = {t for t in ([prop.get("type")] + [a.get("type") for a in prop.get("anyOf", [])]) if t and t != "null"}
        if prop.get("enum"):
            kind, choices = "enum", tuple(str(c) for c in prop["enum"])
        elif types == {"boolean"}:
            kind = "bool"
        elif types and types <= {"integer", "number"}:
            kind, integer = "number", types == {"integer"}
        elif types == {"string"}:
            kind = "text"
        elif types == {"array"} and (prop.get("items") or {}).get("type") == "string":
            kind = "list"
        out.append(Field(name, prop.get("title") or name, kind, choices, prop.get("description", ""), name in required, integer, prop.get("default")))
    return out


def display(field: Field, value: Any) -> str:
    """A value as the form box shows it."""
    if value is None:
        return ""
    if field.kind == "list":
        return "\n".join(str(v) for v in value) if isinstance(value, list) else str(value)
    if field.kind == "json":
        return json.dumps(value, indent=2, sort_keys=True)
    return str(value)


def parse(fields: list[Field], form: Mapping[str, str]) -> dict[str, Any]:
    """The typed values a posted form holds. An empty box is absent (so a default applies), except a
    checkbox, which is false when unchecked. Raises ``InvalidInput`` naming the field."""
    out: dict[str, Any] = {}
    for f in fields:
        raw = form.get(f"f_{f.name}", "")
        if f.kind == "bool":
            out[f.name] = f"f_{f.name}" in form
            continue
        if raw.strip() == "":
            if f.required:
                raise InvalidInput(f"{f.label}: required")
            continue
        try:
            if f.kind == "number":
                out[f.name] = int(raw) if f.integer or raw.strip().lstrip("-").isdigit() else float(raw)
            elif f.kind == "list":
                out[f.name] = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            elif f.kind == "json":
                out[f.name] = json.loads(raw)
            elif f.kind == "enum":
                if raw not in f.choices:
                    raise InvalidInput(f"{f.label}: pick one of {', '.join(f.choices)}")
                out[f.name] = raw
            else:
                out[f.name] = raw
        except ValueError:
            raise InvalidInput(f"{f.label}: not a valid {f.kind}") from None
    return out
