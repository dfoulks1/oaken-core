# SPDX-License-Identifier: Apache-2.0
import json
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel

from oaken_core.api import Api
from oaken_core.cli.client import ApiClient
from oaken_core.cli.commands import amain
from oaken_core.cli.discovery import Endpoint
from oaken_core.cli.exits import CoreUnavailable
from oaken_core.cli.http import HttpApiClient
from oaken_core.cli.inprocess import InProcessApiClient
from oaken_core.operations.registry import Operation

from .conftest import Ops


class HiIn(BaseModel):
    who: str = "you"
    shout: bool = False


class HiOut(BaseModel):
    text: str


async def _hi(ctx: Any, inp: HiIn) -> HiOut:
    return HiOut(text=("HI " if inp.shout else "hi ") + inp.who)


def greeter() -> list[Operation[Any, Any]]:
    return [Operation(name="greeter.hi", summary="Say hi.", permission="read", input=HiIn, output=HiOut,
                      handler=_hi, mcp="off", plugin="greeter")]  # fmt: skip


async def run(ops: Ops, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = await amain(list(argv), prog="core", client_factory=lambda _a: InProcessApiClient(ops.client))
    out = capsys.readouterr()
    return code, out.out, out.err


async def test_the_command_tree_is_the_registry_and_a_plugin_verb_appears_only_while_it_is_loaded(ops: Ops, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = await run(ops, capsys, "operations", "--json")
    names = {r["command"] for r in json.loads(out)}
    assert code == 0 and {"queue requeue", "queue pause", "agent create", "label set", "status", "plugin add"} <= names
    assert "greeter hi" not in names  # the plugin has not loaded
    ops.registry.attach_plugins(greeter)  # it loads
    code, out, _ = await run(ops, capsys, "greeter", "hi", "--who", "Drew", "--shout", "--json")
    assert code == 0 and json.loads(out)["output"] == {"text": "HI Drew"}
    ops.registry.attach_plugins(lambda: [])  # it failed to load, or was disabled
    code, _, err = await run(ops, capsys, "greeter", "hi")
    assert code == 2 and "invalid choice" in err


async def test_a_change_is_a_proposal_until_yes_and_exit_codes_say_which(ops: Ops, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = await run(ops, capsys, "queue", "pause", "--queue", "default")
    assert code == 5 and "would: Pause queue default" in out and "re-run with --yes" in out
    assert not (await ops.kit.jobs.stats())[0].paused
    code, out, _ = await run(ops, capsys, "queue", "pause", "--queue", "default", "--yes")
    assert code == 0 and (await ops.kit.jobs.stats())[0].paused


async def test_flags_come_from_the_schema_and_json_input_fills_the_rest(ops: Ops, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = await run(ops, capsys, "queue", "pause")  # --queue is required
    assert code == 2 and "needs --queue" in err
    code, out, _ = await run(ops, capsys, "queue", "pause", "--json-input", '{"queue": "default"}', "--yes", "--json")
    assert code == 0 and json.loads(out)["output"] == {"queue": "default", "paused": True}
    code, _, err = await run(ops, capsys, "queue", "pause", "--json-input", "[1]")
    assert code == 2 and "JSON object" in err
    code, _, err = await run(ops, capsys, "queue", "pause", "--queue", "nope", "--yes")
    assert code == 2 and "no queue named" in err  # InvalidInput maps to usage


async def test_labels_through_the_cli_and_a_list_prints_a_table(ops: Ops, capsys: pytest.CaptureFixture[str]) -> None:
    code, *_ = await run(ops, capsys, "label", "set", "--subject-id", "s1", "--key", "priority", "--value", "urgent", "--yes")
    assert code == 0
    code, out, _ = await run(ops, capsys, "label", "get", "--subject-id", "s1", "--json")
    assert json.loads(out)["output"]["labels"][0]["human"] == "urgent"  # a person's value: the human layer
    code, out, _ = await run(ops, capsys, "queue", "list")
    assert code == 0 and "default" in out and out.splitlines()[0].startswith("queue")  # a table with a header


async def test_describe_shows_the_schema_and_a_bad_verb_is_usage(ops: Ops, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = await run(ops, capsys, "describe", "queue_requeue")
    assert code == 0 and "retriage" in json.loads(out)["input"]["properties"]
    code, _, _ = await run(ops, capsys, "describe", "nope")
    assert code == 2
    code, _, _ = await run(ops, capsys, "frobnicate")
    assert code == 2


async def test_core_down_is_exit_3_with_one_clean_line(ops: Ops, capsys: pytest.CaptureFixture[str]) -> None:
    def down(_a: Any) -> ApiClient:
        raise CoreUnavailable("no runtime file")

    code = await amain(["status"], prog="core", client_factory=down)
    err = capsys.readouterr().err
    assert code == 3 and err.strip() == "core is not running"


async def test_the_same_commands_work_over_http(ops: Ops, capsys: pytest.CaptureFixture[str]) -> None:
    api = Api(ops.client, token="t")
    api.bind(8123)

    def over_http(_a: Any) -> ApiClient:
        return HttpApiClient(Endpoint("http://127.0.0.1:8123", "t"), transport=httpx2.ASGITransport(app=api))

    ops.registry.attach_plugins(greeter)
    code = await amain(["greeter", "hi", "--who", "web", "--json"], prog="core", client_factory=over_http)
    assert code == 0 and json.loads(capsys.readouterr().out)["output"] == {"text": "hi web"}
    code = await amain(["queue", "pause", "--queue", "default"], prog="core", client_factory=over_http)
    assert code == 5  # the proposal survives the wire, so does exit 5
    bad = lambda _a: HttpApiClient(Endpoint("http://127.0.0.1:8123", "wrong"), transport=httpx2.ASGITransport(app=api))  # noqa: E731
    capsys.readouterr()
    assert await amain(["status"], prog="core", client_factory=bad) == 4  # a refused token is exit 4


def test_a_non_loopback_url_needs_remote() -> None:
    from oaken_core.cli.discovery import discover
    from oaken_core.cli.exits import CliError

    with pytest.raises(CliError, match="--remote"):
        discover(url="http://10.0.0.9:8123", token="t")
    assert discover(url="http://10.0.0.9:8123", token="t", allow_remote=True).base_url == "http://10.0.0.9:8123"


async def test_help_lists_core_s_own_commands_even_when_no_core_is_running(capsys: pytest.CaptureFixture[str]) -> None:
    def down(_a: Any) -> ApiClient:
        raise CoreUnavailable()

    for argv in ([], ["--help"], ["plugin", "--help"]):
        code = await amain(argv, prog="oaken", client_factory=down)
        out = capsys.readouterr()
        assert code == 0, argv
        assert {"start", "stop", "plugin", "queue", "status"} <= set(out.out.split()), argv
    code = await amain([], prog="oaken", client_factory=down)
    assert "Core is not running" in capsys.readouterr().out
    assert await amain(["status"], prog="oaken", client_factory=down) == 3  # a real command still exits 3


def test_a_field_marked_positional_is_the_commands_argument_not_a_flag() -> None:
    from oaken_core.cli.schema_flags import missing_required, parse_flags
    from oaken_core.operations.builtin.system import PluginAddIn

    schema = PluginAddIn.model_json_schema()
    assert parse_flags(schema, ["oaken-triager-crm", "--name", "crm2"], prog="plugin add") == {"plugin": "oaken-triager-crm", "name": "crm2"}
    assert missing_required(schema, {}) == ["plugin"]  # named as the argument, not as --plugin


def test_a_field_marked_stdin_is_filled_from_a_pipe_when_no_flag_gave_it(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse
    import io

    from oaken_core.cli.client import OpInfo
    from oaken_core.cli.commands import _payload

    schema = {"properties": {"text": {"type": "string", "stdin": True}, "source": {"type": "string"}}, "required": ["text"]}
    op = OpInfo(name="p.ingest", summary="", permission="mutate", input_schema=schema)
    piped = io.StringIO('{"a": 1}\n{"a": 2}\n')
    piped.isatty = lambda: False  # type: ignore[method-assign]
    monkeypatch.setattr("sys.stdin", piped)
    assert _payload(argparse.Namespace(source="x"), op) == {"text": '{"a": 1}\n{"a": 2}\n', "source": "x"}
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # a terminal: nothing to read, so the field is missing
    tty = io.StringIO("")
    tty.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr("sys.stdin", tty)
    with pytest.raises(Exception, match="--text"):
        _payload(argparse.Namespace(), op)


def test_a_template_instances_op_is_three_levels_and_a_plain_plugins_is_two() -> None:
    """A template plugin's operation addresses as ``oaken <template> <instance> <verb>``; a plain
    plugin stays ``oaken <plugin> <verb>``. The third level is the ``template`` field; the op's own
    name (and so its dispatch) is unchanged (``<instance>.<verb>``)."""
    from oaken_core.cli.client import OpInfo

    tmpl = OpInfo(name="warwalrux.auth", summary="sign in", permission="mutate", plugin="warwalrux", template="gmail")
    plain = OpInfo(name="crm.scan", summary="scan", permission="read", plugin="crm")
    assert tmpl.cli_path == ("gmail", "warwalrux", "auth")  # template -> instance -> verb
    assert plain.cli_path == ("crm", "scan")  # unchanged two-level


def test_the_three_level_template_tree_routes_to_the_right_leaf() -> None:
    """``build_parser`` nests template -> instance -> verb, and ``oaken gmail warwalrux auth`` reaches
    that op's leaf. Two instances of the same template share the ``gmail`` group."""
    from oaken_core.cli.client import OpInfo
    from oaken_core.cli.commands import build_parser

    ops = [
        OpInfo(name="warwalrux.auth", summary="sign in", permission="mutate", plugin="warwalrux", template="gmail"),
        OpInfo(name="warwalrux.check", summary="check", permission="read", plugin="warwalrux", template="gmail"),
        OpInfo(name="dfoulks.auth", summary="sign in", permission="mutate", plugin="dfoulks", template="gmail"),
        OpInfo(name="crm.scan", summary="scan", permission="read", plugin="crm"),  # a plain plugin: two levels
    ]
    parser = build_parser("oaken", ops)
    args = parser.parse_args(["gmail", "warwalrux", "auth"])
    assert getattr(args, "_op").name == "warwalrux.auth"
    args = parser.parse_args(["gmail", "dfoulks", "auth"])
    assert getattr(args, "_op").name == "dfoulks.auth"
    args = parser.parse_args(["crm", "scan"])  # plain plugin unchanged
    assert getattr(args, "_op").name == "crm.scan"
