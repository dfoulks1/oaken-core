# SPDX-License-Identifier: Apache-2.0
"""An OpenAPI 3.1 document derived from the registry. Nothing in it is written by hand: every
schema comes from an operation's pydantic models, so the document cannot drift from the code.

Each operation is ``POST /v1/operations/<name>``. The body is ``{"input": ..., "confirm": ...,
"confirmation_token": ...}``; the answer is ``{"status": "ok", "output": ...}`` or, for a mutation
that still needs confirming, ``{"status": "confirmation_required", "proposal": ...}``.
"""

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

from oaken_core.operations.gate import Proposal
from oaken_core.operations.registry import AnyOperation, Change

REF = "#/components/schemas/{model}"


def _camel(name: str) -> str:
    return "".join(p.capitalize() for p in name.replace(".", "_").split("_"))


def _add(schemas: dict[str, Any], key: str, schema: dict[str, Any]) -> dict[str, str]:
    """Put ``schema`` (and the ``$defs`` pydantic hoisted out of it) under ``schemas``; return a
    ``$ref`` to it. Two different models claiming one name is an error, not a silent overwrite."""
    schema = dict(schema)
    for name, sub in schema.pop("$defs", {}).items():
        if name in schemas and schemas[name] != sub:
            raise ValueError(f"two different models are both named {name!r}; rename one")
        schemas[name] = sub
    if key in schemas and schemas[key] != schema:
        raise ValueError(f"two different models are both named {key!r}; rename one")
    schemas[key] = schema
    return {"$ref": REF.format(model=key)}


def _model(schemas: dict[str, Any], model: type[BaseModel], key: str) -> dict[str, str]:
    return _add(schemas, key, model.model_json_schema(ref_template=REF))


def build_openapi(
    operations: Iterable[AnyOperation], *, title: str = "Oaken Mail", version: str = "1"
) -> dict[str, Any]:
    schemas: dict[str, Any] = {}
    proposal = _model(schemas, Proposal, "Proposal")
    _model(schemas, Change, "Change")
    paths: dict[str, Any] = {}
    error = {"$ref": REF.format(model="Error")}
    for op in sorted(operations, key=lambda o: o.name):
        base = _camel(op.name)
        inp = _model(schemas, op.input, f"{base}Input")
        out = _model(schemas, op.output, f"{base}Output")
        mutate = op.permission == "mutate"
        request = _add(
            schemas,
            f"{base}Request",
            {
                "type": "object",
                "required": ["input"],
                "properties": {
                    "input": inp,
                    **(
                        {
                            "confirm": {
                                "type": "boolean",
                                "default": False,
                                "description": "The person at this client asked for exactly this.",
                            },
                            "confirmation_token": {"type": ["string", "null"]},
                        }
                        if mutate
                        else {}
                    ),
                },
            },
        )
        response_props: dict[str, Any] = {
            "request_id": {"type": "string"},
            "status": {"enum": ["ok", "confirmation_required"] if mutate else ["ok"]},
            "output": {"anyOf": [out, {"type": "null"}]},
        }
        if mutate:
            response_props["proposal"] = {"anyOf": [proposal, {"type": "null"}]}
        response = _add(
            schemas,
            f"{base}Response",
            {"type": "object", "required": ["request_id", "status"], "properties": response_props},
        )
        paths[f"/v1/operations/{op.name}"] = {
            "post": {
                "operationId": op.name.replace(".", "_"),
                "summary": op.summary,
                "description": op.description or op.summary,
                "tags": [op.plugin or "core"],
                "x-oaken-permission": op.permission,
                "x-oaken-marks-read": op.marks_read,
                "x-oaken-mcp": op.mcp,
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": request}},
                },
                "responses": {
                    "200": {
                        "description": "The result.",
                        "content": {"application/json": {"schema": response}},
                    },
                    "default": {
                        "description": "An error with a stable code.",
                        "content": {"application/json": {"schema": error}},
                    },
                },
            }
        }
    schemas["Error"] = {
        "type": "object",
        "required": ["code", "message"],
        "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version},
        "paths": paths,
        "components": {"schemas": schemas},
    }
