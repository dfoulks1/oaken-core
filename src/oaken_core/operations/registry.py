# SPDX-License-Identifier: Apache-2.0
"""The operation registry: every user-facing capability, defined once, as a typed operation.

The general API, the CLI and the MCP server are generated from this registry, so they cannot drift
apart, and the safety rules live here, in code: a permission class on every operation, a confirm
gate on every mutation, and a ceiling on what may ever be offered over MCP.

    @operation("label_set", "Set a label.", permission="mutate", group="label",
               input=LabelSetIn, output=LabelSetOut, mcp="off", preview=preview_label_set)
    async def label_set(ctx: OpContext, inp: LabelSetIn) -> LabelSetOut: ...

Defining an operation that breaks a rule raises at once, at import: a ``send`` operation that could
be offered over MCP, a mutation an agent could reach that cannot say
what it would change.
"""

import re
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar

from pydantic import BaseModel

from oaken_core.errors import NotFound

if TYPE_CHECKING:
    from oaken_core.operations.context import OpContext

Permission = Literal["read", "mutate", "send"]
# ``never``: cannot be offered over MCP, whatever the user sets (the ceiling, fixed in code).
# ``off``: may be offered, and is not until the user turns it on. ``on``: offered by default.
McpMode = Literal["never", "off", "on"]
# Who writes the audit row: the registry (a generic one, after the handler) or the service the
# handler calls (which already records every change it makes, atomically). ``always``: the registry
# writes its row for an operation that is not a mail mutation (an agent working the job queue), so
# what an agent did is on the audit log even though it changes no mail.
AuditBy = Literal["registry", "service", "always"]

In = TypeVar("In", bound=BaseModel)
Out = TypeVar("Out", bound=BaseModel)

NAME = re.compile(r"^[a-z][a-z0-9_]*$")
PLUGIN_SEP = "."


class OperationDefinitionError(ValueError):
    """An operation was defined in a way the rules forbid."""


class Change(BaseModel):
    """One line of a proposal: what would change. ``target`` is an id, never mail text."""

    target: str
    field: str = ""
    before: Any = None
    after: Any = None


class Preview(BaseModel):
    """What a mutation would do, shown before anything happens. ``changes`` may be a prefix of the
    whole (the proposal limit); ``total_changes`` is always the real count."""

    summary: str
    changes: list[Change] = []
    total_changes: int = 0


Handler = Callable[["OpContext", In], Awaitable[Out]]
PreviewFn = Callable[["OpContext", In], Awaitable[Preview]]


@dataclass(frozen=True)
class Operation(Generic[In, Out]):
    name: str
    summary: str
    permission: Permission
    input: type[In]
    output: type[Out]
    handler: Handler[In, Out]
    mcp: McpMode = "never"
    streaming: bool = False  # long-running: the result is a job or an event stream
    preview: PreviewFn[In] | None = None
    audit: AuditBy = "registry"
    plugin: str | None = None  # the plugin that offers it (its name is the name's prefix)
    description: str = ""
    # The template this is an instance of (``gmail`` for a ``warwalrux`` instance), set when the
    # plugin is a template. It names the CLI group: ``oaken <template> <instance> <verb>``. None for
    # a plain plugin, which stays two-level (``oaken <plugin> <verb>``).
    template: str | None = None
    # The CLI group a core operation sits under (``queue`` for ``queue_requeue``: ``queue requeue``).
    # A plugin operation's group is its plugin (``<plugin>.<name>`` is ``<plugin> <name>``).
    group: str = ""
    # An agent may call it with no grant, because the token's own scope bounds it (the job protocol:
    # claim, heartbeat, complete, fail). Every other operation an agent calls must be granted.
    agent_ok: bool = False

    def __post_init__(self) -> None:
        base = self.name.split(PLUGIN_SEP, 1)
        if self.plugin is None:
            if len(base) != 1 or not NAME.match(self.name):
                raise OperationDefinitionError(f"bad operation name {self.name!r}")
        elif len(base) != 2 or base[0] != self.plugin or not NAME.match(base[1]):
            raise OperationDefinitionError(
                f"a plugin operation is <plugin>.<name>, got {self.name!r} for {self.plugin!r}"
            )
        if self.permission not in ("read", "mutate", "send"):
            raise OperationDefinitionError(f"{self.name}: unknown permission {self.permission!r}")
        if self.permission == "send" and self.mcp != "never":
            raise OperationDefinitionError(f"{self.name}: sending is never exposed over MCP")
        if self.permission == "mutate" and self.mcp != "never" and self.preview is None:
            raise OperationDefinitionError(
                f"{self.name}: an operation an agent can reach must preview what it would change"
            )
        if self.group and not self.name.startswith(self.group + "_"):
            raise OperationDefinitionError(f"{self.name}: its group {self.group!r} must prefix it")
        if self.plugin is not None and self.mcp == "on":
            raise OperationDefinitionError(
                f"{self.name}: a plugin's operations start off over MCP"
            )

    @property
    def cli_path(self) -> tuple[str, ...]:
        """Where the CLI puts it: ``(template, instance, verb)`` for a template instance's
        operation (``oaken gmail warwalrux auth``), ``(group, verb)`` for a core operation in a
        group, ``(plugin, verb)`` for a plain plugin's, ``(name,)`` for a core operation with none."""
        if self.plugin is not None:
            verb = self.name.split(PLUGIN_SEP, 1)[1]
            if self.template is not None:
                return (self.template, self.plugin, verb)
            return (self.plugin, verb)
        if self.group:
            return (self.group, self.name[len(self.group) + 1 :])
        return (self.name,)

    @property
    def mcp_allowed(self) -> bool:
        """The ceiling: may the user ever turn this on for MCP?"""
        return self.mcp != "never"

    @property
    def mcp_exposed(self) -> bool:
        """Offered over MCP unless the user says otherwise."""
        return self.mcp == "on"

    def input_schema(self) -> dict[str, Any]:
        return self.input.model_json_schema()

    def output_schema(self) -> dict[str, Any]:
        return self.output.model_json_schema()


AnyOperation = Operation[Any, Any]


def operation(
    name: str,
    summary: str,
    *,
    permission: Permission,
    input: type[In],
    output: type[Out],
    mcp: McpMode = "never",
    streaming: bool = False,
    preview: PreviewFn[In] | None = None,
    audit: AuditBy = "registry",
    group: str = "",
    agent_ok: bool = False,
) -> Callable[[Handler[In, Out]], Operation[In, Out]]:
    """Turn a handler into an ``Operation`` (it is not registered until the registry is built)."""

    def wrap(fn: Handler[In, Out]) -> Operation[In, Out]:
        return Operation(
            name=name,
            summary=summary,
            permission=permission,
            input=input,
            output=output,
            handler=fn,
            mcp=mcp,
            streaming=streaming,
            preview=preview,
            audit=audit,
            group=group,
            agent_ok=agent_ok,
            description=" ".join((fn.__doc__ or "").split()),
        )

    return wrap


PluginSource = Callable[[], Sequence[AnyOperation]]


class OperationRegistry:
    """The core operations, plus those the loaded plugins offer (looked up live, because a plugin
    can go unhealthy and come back)."""

    def __init__(self, operations: Sequence[AnyOperation] = ()) -> None:
        self._ops: dict[str, AnyOperation] = {}
        self._plugins: PluginSource = lambda: ()
        for op in operations:
            self.register(op)

    def register(self, op: AnyOperation) -> None:
        if op.plugin is not None:
            raise OperationDefinitionError(
                f"{op.name}: plugin operations come from the plugin host"
            )
        if op.name in self._ops:
            raise OperationDefinitionError(f"operation {op.name!r} is defined twice")
        self._ops[op.name] = op

    def attach_plugins(self, source: PluginSource) -> None:
        """Offer the operations ``source()`` returns, beside the core ones."""
        self._plugins = source

    def _plugin_ops(self) -> dict[str, AnyOperation]:
        found: dict[str, AnyOperation] = {}
        for op in self._plugins():
            if op.name not in self._ops and op.name not in found:
                found[op.name] = op
        return found

    def get(self, name: str) -> AnyOperation:
        op = self._ops.get(name) or self._plugin_ops().get(name)
        if op is None:
            raise NotFound(f"no such operation: {name}")
        return op

    def all(self) -> list[AnyOperation]:
        return sorted([*self._ops.values(), *self._plugin_ops().values()], key=lambda o: o.name)

    def core(self) -> list[AnyOperation]:
        return sorted(self._ops.values(), key=lambda o: o.name)

    def names(self) -> list[str]:
        return [o.name for o in self.all()]

    def __iter__(self) -> Iterator[AnyOperation]:
        return iter(self.all())

    def __len__(self) -> int:
        return len(self.all())

    def __contains__(self, name: object) -> bool:
        return name in self._ops or name in self._plugin_ops()
