# SPDX-License-Identifier: Apache-2.0
"""Command-line flags from an operation's input JSON Schema.

Every operation, core or plugin, publishes the schema of its input, so the CLI never needs a
hand-written verb to reach one: ``<prog> <plugin> <verb> --some-flag x`` is built from that schema, including the verbs
plugins add when they load. A field becomes a flag
(``foo_bar`` is ``--foo-bar``); a list is a repeated flag; a boolean is ``--x/--no-x``; an enum
limits the choices; a nested object is given as JSON. Required fields are required flags.
A field marked ``writeOnly`` is a secret: it gets no value flag (a flag lands in shell history
and process lists), only ``--NAME-stdin``, and the caller asks for it without echo.
Hand-written verbs stay as the friendlier front end for the operations people use daily.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

_SCALARS: dict[str, Any] = {"string": str, "integer": int, "number": float}


def _json_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"not valid JSON: {exc}") from None


def _resolve(prop: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Follow a ``$ref`` and unwrap ``Optional`` (``anyOf`` with null) to the one real type."""
    seen = 0
    while seen < 8:
        seen += 1
        ref = prop.get("$ref")
        if isinstance(ref, str) and ref.rsplit("/", 1)[-1] in defs:
            prop = {
                **defs[ref.rsplit("/", 1)[-1]],
                **{k: v for k, v in prop.items() if k != "$ref"},
            }
            continue
        options = [o for o in prop.get("anyOf", []) if o.get("type") != "null"]
        if len(options) == 1 and "type" not in prop:
            prop = {**{k: v for k, v in prop.items() if k != "anyOf"}, **options[0]}
            continue
        break
    return prop


def add_flags(
    parser: argparse.ArgumentParser, schema: dict[str, Any], *, enforce_required: bool = True
) -> list[str]:
    """Add one flag per property of ``schema`` to ``parser``. Returns the field names."""
    defs = schema.get("$defs", {})
    required = set(schema.get("required", []))
    fields: list[str] = []
    for name, raw in schema.get("properties", {}).items():
        prop = _resolve(raw, defs)
        flag = "--" + name.replace("_", "-")
        if raw.get("writeOnly") or prop.get("writeOnly"):
            parser.add_argument(
                f"{flag}-stdin",
                dest=f"_stdin_{name}",
                action="store_true",
                default=False,
                help=f"Read {name} from one line of standard input.",
            )
            fields.append(name)
            continue
        kind = prop.get("type")
        helptext = str(raw.get("description") or prop.get("description") or "")
        kw: dict[str, Any] = {"dest": name, "help": helptext or None}
        if name in required and enforce_required:
            kw["required"] = True
        if kind == "boolean":
            kw["action"] = argparse.BooleanOptionalAction
            kw["default"] = argparse.SUPPRESS
        elif kind == "array":
            item = _resolve(prop.get("items", {}), defs)
            kw["action"] = "append"
            kw["type"] = _SCALARS.get(str(item.get("type")), _json_value)
            if "enum" in item:
                kw["choices"] = item["enum"]
            kw["help"] = (helptext + " (repeat the flag for more)").strip()
        elif kind in _SCALARS:
            kw["type"] = _SCALARS[kind]
            if "enum" in prop:
                kw["choices"] = prop["enum"]
        else:  # an object, a mapping, or a shape we do not model: JSON
            kw["type"] = _json_value
            kw["metavar"] = "JSON"
        if "action" not in kw or kw["action"] == "append":
            kw.setdefault("default", argparse.SUPPRESS)
        parser.add_argument(flag, **kw)
        fields.append(name)
    return fields


def parse_flags(
    schema: dict[str, Any], argv: list[str], *, prog: str, enforce_required: bool = True
) -> dict[str, Any]:
    """The operation input given by ``argv``'s flags (only the ones that were given). A bad flag
    exits with argparse's usage message (exit code 2)."""
    parser = argparse.ArgumentParser(prog=prog, add_help=True)
    fields = add_flags(parser, schema, enforce_required=enforce_required)
    ns = parser.parse_args(argv)
    out = {f: getattr(ns, f) for f in fields if hasattr(ns, f)}
    for f in fields:
        if getattr(ns, f"_stdin_{f}", False):
            out[f] = sys.stdin.readline().rstrip("\r\n")
    return out


def secret_fields(schema: dict[str, Any]) -> list[str]:
    """The fields that are secrets (``writeOnly``): never a flag, asked for without echo."""
    return [
        name
        for name, raw in schema.get("properties", {}).items()
        if raw.get("writeOnly") or _resolve(raw, schema.get("$defs", {})).get("writeOnly")
    ]


def missing_required(schema: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    """The required flags (``--account-id``) that neither a flag nor ``--json-input`` supplied."""
    return [
        "--" + name.replace("_", "-")
        for name in schema.get("required", [])
        if name not in payload and name not in secret_fields(schema)
    ]
