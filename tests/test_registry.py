# SPDX-License-Identifier: Apache-2.0
"""The registry's own rules: what every operation must have, and what cannot be defined at all."""

import re

import pytest
from openapi_spec_validator import validate
from pydantic import BaseModel

from oaken_core.operations.builtin import core_operations, core_registry
from oaken_core.operations.context import OpContext
from oaken_core.operations.openapi import _add, build_openapi
from oaken_core.operations.registry import Operation, OperationDefinitionError, operation

from .conftest import Ops


class Empty(BaseModel):
    pass


async def _noop(ctx: OpContext, inp: Empty) -> Empty:
    return Empty()


def define(name: str = "thing", **kw: object) -> Operation[Empty, Empty]:
    args: dict[str, object] = {
        "name": name, "summary": "x", "permission": "read", "input": Empty, "output": Empty, "handler": _noop,
    }
    return Operation(**{**args, **kw})  # type: ignore[arg-type]


def test_every_core_operation_has_models_a_permission_and_a_generated_schema() -> None:
    ops = core_operations()
    assert len(ops) >= 20
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
    define("send_message", permission="send", mcp="never")  # defined, but disabled
    assert not any(o.permission == "send" and o.mcp_allowed for o in core_operations())


def test_a_mutation_an_agent_can_reach_must_say_what_it_would_change() -> None:
    with pytest.raises(OperationDefinitionError, match="preview"):
        define(permission="mutate", mcp="on")
    define(permission="mutate", mcp="never")  # a person's own action needs no preview


def test_a_plugin_operation_is_named_for_its_plugin_and_starts_off() -> None:
    with pytest.raises(OperationDefinitionError, match="<plugin>.<name>"):
        define("hello", plugin="greeter")
    with pytest.raises(OperationDefinitionError, match="start off"):
        define("greeter.hello", plugin="greeter", mcp="on")
    define("greeter.hello", plugin="greeter", mcp="off")


def test_the_declared_rules_hold_for_the_real_operations() -> None:
    for op in core_operations():
        if op.permission == "mutate" and op.mcp_allowed:
            assert op.preview is not None, op.name


def test_what_is_never_offered_over_mcp_is_what_the_owner_was_promised() -> None:
    never = {o.name for o in core_operations() if not o.mcp_allowed}
    assert {
        "agent_create", "agent_grant", "agent_revoke", "agent_list", "exposure_set", "exposure_list",
        "settings_set", "label_clear", "plugin_add", "plugin_enable", "plugin_disable", "core_reload",
    } <= never  # fmt: skip


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
    pause = doc["paths"]["/v1/operations/queue_pause"]["post"]
    assert pause["x-oaken-permission"] == "mutate"
    props = doc["components"]["schemas"]["QueuePauseRequest"]["properties"]
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
    hello = define("greeter.hello", plugin="greeter", mcp="off")
    ops.registry.attach_plugins(lambda: [hello])
    names = ops.registry.names()
    assert "greeter.hello" in names and "queue_pause" in names
    assert names == sorted(names)
    assert "greeter.hello" not in {o.name for o in ops.registry.core()}
    ops.registry.attach_plugins(lambda: [])  # a plugin that goes away is gone at once
    assert "greeter.hello" not in ops.registry
