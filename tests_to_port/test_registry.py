# SPDX-License-Identifier: Apache-2.0
"""The registry's own rules: what every operation must have, and what cannot be defined at all."""

import re

import pytest
from openapi_spec_validator import validate
from pydantic import BaseModel

from oaken_core.operations import OpContext, Operation, OperationDefinitionError
from oaken_core.operations.builtin import core_operations, core_registry
from oaken_core.operations.openapi import _add, build_openapi
from oaken_core.operations.registry import operation
from oaken_core.operations.tests.conftest import Ops


class Empty(BaseModel):
    pass


async def _noop(ctx: OpContext, inp: Empty) -> Empty:
    return Empty()


def define(name: str = "thing", **kw: object) -> Operation[Empty, Empty]:
    args: dict[str, object] = {
        "name": name,
        "summary": "x",
        "permission": "read",
        "input": Empty,
        "output": Empty,
        "handler": _noop,
    }
    return Operation(**{**args, **kw})  # type: ignore[arg-type]


def test_every_core_operation_has_models_a_permission_and_a_generated_schema() -> None:
    ops = core_operations()
    assert len(ops) >= 30
    for op in ops:
        assert issubclass(op.input, BaseModel) and issubclass(op.output, BaseModel), op.name
        assert op.permission in ("read", "mutate", "send"), op.name
        assert op.summary and op.summary.endswith((".", "?")), op.name
        assert op.input_schema()["type"] == "object", op.name
        assert op.output_schema()["type"] == "object", op.name
        assert re.fullmatch(r"[a-z][a-z0-9_]*", op.name), op.name


def test_operation_names_are_unique_and_a_duplicate_is_refused() -> None:
    names = [o.name for o in core_operations()]
    assert len(names) == len(set(names))
    registry = core_registry()
    with pytest.raises(OperationDefinitionError, match="twice"):
        registry.register(registry.get("status"))


def test_a_send_operation_that_could_be_exposed_over_mcp_cannot_be_defined() -> None:
    for mode in ("on", "off"):
        with pytest.raises(OperationDefinitionError, match="never exposed"):
            define("send_message", permission="send", mcp=mode)
    define("send_message", permission="send", mcp="never")  # defined, but disabled in v1
    assert not any(o.permission == "send" and o.mcp_allowed for o in core_operations())


def test_a_read_operation_cannot_claim_to_mark_mail_read() -> None:
    with pytest.raises(OperationDefinitionError, match="cannot mark"):
        define(permission="read", marks_read=True)


def test_a_mutation_an_agent_can_reach_must_say_what_it_would_change() -> None:
    with pytest.raises(OperationDefinitionError, match="preview"):
        define(permission="mutate", mcp="on")
    define(permission="mutate", mcp="never")  # a person's own action needs no preview


def test_the_declared_rules_hold_for_the_real_operations() -> None:
    for op in core_operations():
        if op.permission == "mutate" and op.mcp_allowed:
            assert op.preview is not None, op.name
        if op.permission == "read":
            assert not op.marks_read, op.name
    marking = {o.name for o in core_operations() if o.marks_read}
    assert marking == {
        "mark_read", "mark_unread", "undo", "message_presented", "summarize_message",
    }  # fmt: skip


def test_what_is_never_offered_over_mcp_is_what_the_user_was_promised() -> None:
    never = {o.name for o in core_operations() if not o.mcp_allowed}
    assert {
        "get_raw", "reprocess", "rebuild_index", "set_schedule", "pause_schedule",
        "resume_schedule", "remove_schedule", "set_operation_exposure", "list_operations",
        "list_audit", "message_presented", "message_dismissed",
    } <= never  # fmt: skip
    # Sieve scripts are the person's own configuration: writing one is a local, confirmed,
    # audited action (prompt 26) and reading/testing one is local too. Either way, no sieve
    # operation is ever offered to an agent over MCP.
    sieve = {o.name: o for o in core_operations() if "sieve" in o.name}
    assert sieve and all(not o.mcp_allowed for o in sieve.values())
    assert {"sieve_check", "sieve_test", "sieve_list", "sieve_get"} <= set(sieve)
    assert all(sieve[n].permission == "read" for n in ("sieve_check", "sieve_test", "sieve_list"))
    assert all(sieve[n].permission == "mutate" for n in ("sieve_save", "sieve_delete"))


def test_bad_names_are_refused() -> None:
    for bad in ("Mark", "mark read", "a.b", "", "1x"):
        with pytest.raises(OperationDefinitionError):
            define(bad)


def test_the_decorator_builds_an_operation_without_registering_it() -> None:
    @operation("hello", "Say hi.", permission="read", input=Empty, output=Empty)
    async def hello(ctx: OpContext, inp: Empty) -> Empty:
        """Says hi."""
        return Empty()

    assert isinstance(hello, Operation) and hello.description == "Says hi."
    assert "hello" not in core_registry()


def test_the_openapi_document_generated_from_the_registry_validates() -> None:
    doc = build_openapi(core_registry().all())
    validate(doc)  # raises if it is not a valid OpenAPI 3.1 document
    assert doc["openapi"].startswith("3.1")
    assert len(doc["paths"]) == len(core_operations())
    mark = doc["paths"]["/v1/operations/mark_read"]["post"]
    assert mark["x-oaken-permission"] == "mutate" and mark["x-oaken-marks-read"] is True
    props = doc["components"]["schemas"]["MarkReadRequest"]["properties"]
    assert {"input", "confirm", "confirmation_token"} <= set(props)
    read = doc["components"]["schemas"]["StatusRequest"]["properties"]
    assert "confirm" not in read  # a read cannot be confirmed: there is nothing to confirm


def test_two_different_schemas_under_one_name_are_refused_not_merged() -> None:
    schemas: dict[str, object] = {}
    _add(schemas, "Same", {"type": "object", "properties": {"a": {"type": "integer"}}})
    _add(schemas, "Same", {"type": "object", "properties": {"a": {"type": "integer"}}})  # equal
    with pytest.raises(ValueError, match="both named"):
        _add(schemas, "Same", {"type": "object", "properties": {"b": {"type": "string"}}})
    with pytest.raises(ValueError, match="both named"):
        _add(schemas, "Other", {"$defs": {"Same": {"type": "string"}}, "type": "object"})


async def test_the_registry_serves_plugin_operations_beside_core_ones(ops: Ops) -> None:
    names = ops.registry.names()
    assert "greeter.hello" in names and "mark_read" in names
    assert names == sorted(names)
    assert "greeter.hello" not in {o.name for o in ops.registry.core()}
