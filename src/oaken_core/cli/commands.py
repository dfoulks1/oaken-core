# SPDX-License-Identifier: Apache-2.0
"""The command line, built from the operation registry.

There is one verb per operation and none written by hand: the tree comes from what the core offers
*now* (``GET /v1/operations``, or the in-process registry), so a plugin's commands appear when its
plugin loads (``<prog> <plugin> <verb> --flag ...``), a core operation sits under its group
(``<prog> queue requeue``), and a plugin that failed to load has no verbs. Flags are generated from
each operation's input schema (``schema_flags``).

A mutation is a proposal until confirmed: without ``--yes`` it prints what would change and exits 5.
Exit codes are a contract (``exits``). ``--json`` prints the original data, unsanitized; human
output is sanitized for a terminal.

Adapted from oaken-mail's ``operation`` command and its schema flags (cf3b187); the hand-written
mail verbs are gone, so there is nothing to keep in step with the registry.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from typing import Any

from oaken_core import identity
from oaken_core.cli.client import ApiClient, CallResult, OpInfo
from oaken_core.cli.exits import CliError, CoreUnavailable, ExitCode, exit_for_error_code
from oaken_core.cli.output import print_json, print_table, print_text
from oaken_core.cli.schema_flags import add_flags, missing_required, secret_fields

ClientFactory = Callable[[argparse.Namespace], ApiClient]
ALIASES = {"queue": ["q"]}  # a group answers to its short name too
BUILTIN = ("operations", "describe", "watch")  # verbs that are the CLI's own, not operations
DAEMON = {  # work with no core running, so they are handled before one is asked
    "start": "Run core in the foreground.",
    "stop": "Stop the running core.",
    "reload": "Tell the running core to re-read its settings.",
}


def _globals(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Print the original data as JSON.")
    parser.add_argument("--yes", "-y", action="store_true", help="Confirm a change without asking.")
    parser.add_argument("--json-input", metavar="JSON", help="The operation's input as JSON ('-' reads stdin).")
    parser.add_argument("--url", help="The API address (default: the running core).")
    parser.add_argument("--token-file", help="The API token file.")
    parser.add_argument("--remote", action="store_true", help="Allow a non-loopback --url.")


def build_parser(prog: str, ops: list[OpInfo]) -> argparse.ArgumentParser:
    """The parser for the operations core offers: groups, plugin verbs and flags from schemas."""
    parser = argparse.ArgumentParser(prog=prog, description="Everything core offers, as commands.")
    sub = parser.add_subparsers(dest="verb", metavar="COMMAND")
    sub.add_parser("operations", help="List every operation.", parents=[_parent()])
    desc = sub.add_parser("describe", help="Show one operation's input and output.", parents=[_parent()])
    desc.add_argument("name")
    sub.add_parser("watch", help="Follow core's events as they happen.", parents=[_parent()])
    for verb, text in DAEMON.items():
        sub.add_parser(verb, help=text)
    groups: dict[str, argparse._SubParsersAction[argparse.ArgumentParser]] = {}
    for op in sorted(ops, key=lambda o: o.cli_path):
        path = op.cli_path
        if path[0] in BUILTIN:
            continue
        if len(path) == 1:
            leaf = sub.add_parser(path[0], help=op.summary, parents=[_parent()])
        else:
            if path[0] not in groups:
                group = sub.add_parser(path[0], help=f"{path[0]} commands", aliases=ALIASES.get(path[0], []))
                groups[path[0]] = group.add_subparsers(dest="action", metavar="VERB")
            leaf = groups[path[0]].add_parser(path[1], help=op.summary, parents=[_parent()])
        leaf.set_defaults(_op=op)
        add_flags(leaf, dict(op.input_schema or {}), enforce_required=False)
    return parser


def _parent() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    _globals(p)
    return p


def _payload(args: argparse.Namespace, op: OpInfo) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    raw = getattr(args, "json_input", None)
    if raw == "-":
        raw = sys.stdin.read().strip()
    if raw:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CliError(f"--json-input is not valid JSON: {exc}", ExitCode.USAGE) from None
        if not isinstance(value, dict):
            raise CliError("--json-input must be a JSON object", ExitCode.USAGE)
        payload.update(value)
    schema = dict(op.input_schema or {})
    for name in schema.get("properties", {}):
        if hasattr(args, name):
            payload[name] = getattr(args, name)
        if getattr(args, f"_stdin_{name}", False):
            payload[name] = sys.stdin.readline().rstrip("\r\n")
    if missing := missing_required(schema, payload):
        raise CliError(f"{op.name} needs {', '.join(missing)}", ExitCode.USAGE)
    for name in secret_fields(schema):
        if not payload.get(name):
            payload[name] = _ask_secret(op.name, name)
    return payload


def _ask_secret(operation: str, field: str) -> str:
    """A secret field, without echo, from a terminal. Never from an argument."""
    import getpass

    if not sys.stdin.isatty():
        raise CliError(f"no terminal to ask for {field} on; pipe it with --{field.replace('_', '-')}-stdin", ExitCode.USAGE)
    value = getpass.getpass(f"{operation} {field} (not shown): ")
    if not value:
        raise CliError(f"no {field} given", ExitCode.USAGE)
    return value


def render(args: argparse.Namespace, result: CallResult) -> int:
    """An executed output, or a proposal (exit 5: not done)."""
    if result.proposal is not None:
        p = result.proposal
        if args.json:
            print_json({"request_id": result.request_id, "status": "confirmation_required",
                        "proposal": {"operation": p.operation, "summary": p.summary,
                                     "changes": p.changes, "total_changes": p.total_changes}})  # fmt: skip
        else:
            print_text(f"would: {p.summary}")
            for c in p.changes[:10]:
                print_text(f"  {c.get('target', '')} {c.get('field', '')}: {c.get('before')!r} -> {c.get('after')!r}")
            print_text(f"{p.total_changes} change(s); re-run with --yes to confirm.")
        return int(ExitCode.NEEDS_CONFIRMATION)
    if args.json:
        print_json({"request_id": result.request_id, "output": result.output})
    elif result.output is None:
        print_text("ok")
    else:
        _human(result.output)
    return int(ExitCode.OK)


def _human(output: Any) -> None:
    """A list of records becomes a table; anything else is JSON."""
    if isinstance(output, dict) and len(output) == 1:
        (only,) = output.values()
        if isinstance(only, list) and only and all(isinstance(r, dict) for r in only):
            cols = [k for k, v in only[0].items() if not isinstance(v, dict | list)][:8]
            print_table(only, cols)
            return
    print_json(output)


async def _run(args: argparse.Namespace, client: ApiClient, ops: list[OpInfo]) -> int:
    if args.verb == "operations":
        rows = [{"name": o.name, "command": " ".join(o.cli_path), "permission": o.permission, "summary": o.summary} for o in ops]
        if args.json:
            print_json(rows)
        else:
            print_table(rows, ["command", "permission", "summary"], title="operations")
        return int(ExitCode.OK)
    if args.verb == "describe":
        op = next((o for o in ops if o.name == args.name or " ".join(o.cli_path) == args.name), None)
        if op is None:
            raise CliError(f"no such operation: {args.name}", ExitCode.USAGE)
        data = {"name": op.name, "command": " ".join(op.cli_path), "summary": op.summary,
                "permission": op.permission, "input": op.input_schema, "output": op.output_schema}  # fmt: skip
        print_json(data)
        return int(ExitCode.OK)
    if args.verb == "watch":
        async for event in client.stream_events():
            print_json({"event": event.event, **dict(event.data)}) if args.json else print_text(f"{event.event} {dict(event.data)}")
        return int(ExitCode.OK)
    op = getattr(args, "_op", None)
    if op is None:  # a group with no verb: show the group's help
        return int(ExitCode.USAGE)
    result = await client.call(op.name, _payload(args, op), confirm=bool(args.yes))
    return render(args, result)


def _translate(exc: Exception) -> CliError:
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return CliError(message, exit_for_error_code(code) if isinstance(code, str) else ExitCode.ERROR)


def offline_ops() -> list[OpInfo]:
    """Core's own operations, read from the registry with no core running: what ``--help`` lists
    when there is no daemon to ask. Plugin commands are missing here; they appear while core runs."""
    from oaken_core.operations.builtin import core_registry

    return [
        OpInfo(name=o.name, summary=o.summary, permission=o.permission, group=o.group, plugin=o.plugin,
               agent_ok=o.agent_ok, streaming=o.streaming, input_schema=o.input_schema(),
               output_schema=o.output_schema())
        for o in core_registry().core()
    ]  # fmt: skip


async def amain(argv: list[str], *, prog: str = "oaken", client_factory: ClientFactory | None = None) -> int:
    """Run the CLI and return its exit code. ``client_factory`` is for tests and embedded use; the
    default talks to the running core found through its runtime file."""
    pre = argparse.ArgumentParser(prog=prog, add_help=False)
    _globals(pre)
    known, _ = pre.parse_known_args(argv)

    def make() -> ApiClient:
        if client_factory is not None:
            return client_factory(known)
        from oaken_core.cli.discovery import discover
        from oaken_core.cli.http import HttpApiClient

        return HttpApiClient(discover(url=known.url, token_file=known.token_file, allow_remote=known.remote))

    try:
        async with make() as client:
            ops = await client.list_ops()  # what core offers now: plugins that loaded, and only those
            parser = build_parser(prog, ops)
            args = parser.parse_args(argv)
            if not args.verb:
                parser.print_help()
                return int(ExitCode.OK)
            return await _run(args, client, ops)
    except SystemExit as exc:  # argparse: --help is 0, a usage error is 2
        return int(exc.code or 0) if isinstance(exc.code, int) else int(ExitCode.USAGE)
    except CliError as exc:  # includes CoreUnavailable
        if isinstance(exc, CoreUnavailable) and _wants_help(argv):
            parser = build_parser(prog, offline_ops())
            parser.epilog = "Core is not running: these are core's own commands. A plugin's appear while it runs."
            parser.print_help()
            return int(ExitCode.OK)
        print(exc.message, file=sys.stderr)
        return int(exc.code)
    except Exception as exc:  # noqa: BLE001 - the top of the stack
        err = _translate(exc)
        print(err.message, file=sys.stderr)
        return int(err.code)


def _wants_help(argv: list[str]) -> bool:
    """No command, or a -h/--help: the person is asking what there is, not asking core to do it."""
    return not argv or "-h" in argv or "--help" in argv


def main(argv: list[str] | None = None, *, prog: str | None = None, client_factory: ClientFactory | None = None, make: Any = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("start", "stop", "reload"):  # these run with no core to ask
        from oaken_core.daemon.lifecycle import cli

        return cli(args, make) if make is not None else cli(args)
    return asyncio.run(amain(args, prog=prog or identity.prog, client_factory=client_factory))
