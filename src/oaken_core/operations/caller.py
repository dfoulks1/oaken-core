# SPDX-License-Identifier: Apache-2.0
"""Who is calling an operation, and through which door.

The *client class* decides the rules: a person at the CLI or UI can confirm their own action, an
MCP caller (an agent) can only confirm by presenting a token that a person-visible proposal
produced, and ``internal`` is code running in the daemon. The class is fixed by whoever builds the
``CallerContext`` (the transport), never by anything a caller sends.

So is ``confidential``: whether this caller may be handed an item labelled confidential. The person
at the CLI or console may; an MCP caller may not unless its door says so (``[api] mcp_confidential``
for the person's own MCP door, the agent's ``locality`` on the agent door). The default is no,
because an MCP client can be a hosted model.
"""

from dataclasses import dataclass, field
from typing import Literal

from oaken_core.actors import SYSTEM, USER, Actor
from oaken_core.ids import uuid7

Client = Literal["cli", "ui", "mcp", "internal"]
CLIENTS: tuple[Client, ...] = ("cli", "ui", "mcp", "internal")


def _request_id() -> str:
    return str(uuid7())


@dataclass(frozen=True)
class CallerContext:
    client: Client
    actor: Actor
    request_id: str = field(default_factory=_request_id)
    agent_id: str | None = None  # the swarm agent behind an MCP call on the agent door
    # May this caller be handed confidential items? None: the class's default (yes for the person
    # and core, no for MCP). Set by the transport from the door's own setting, never from input.
    confidential: bool | None = None

    def __post_init__(self) -> None:
        if self.client not in CLIENTS:
            raise ValueError(f"unknown client class {self.client!r}")
        if self.client == "mcp" and self.actor.kind != "agent":
            raise ValueError("an MCP caller is always an agent actor")
        if self.client in ("cli", "ui") and self.actor.kind != "user":
            raise ValueError("the CLI and the UI act for the user")
        if self.confidential is None:
            object.__setattr__(self, "confidential", self.client != "mcp")

    @classmethod
    def cli(cls) -> "CallerContext":
        return cls("cli", USER)

    @classmethod
    def ui(cls) -> "CallerContext":
        return cls("ui", USER)

    @classmethod
    def mcp(
        cls, label: str = "", *, agent_id: str | None = None, confidential: bool = False
    ) -> "CallerContext":
        """An MCP caller. ``confidential`` is what its door allows, and is off unless said."""
        return cls("mcp", Actor("agent", label), agent_id=agent_id, confidential=confidential)

    @classmethod
    def internal(cls, actor: Actor = SYSTEM) -> "CallerContext":
        return cls("internal", actor)

    @property
    def can_confirm(self) -> bool:
        """True if this class may confirm a mutation by saying so. An agent never can."""
        return self.client != "mcp"

    @property
    def may_see_confidential(self) -> bool:
        """True if this caller may be handed an item labelled confidential."""
        return bool(self.confidential)

    @property
    def key(self) -> str:
        """What a confirmation token is bound to: the class and the actor, so a token issued to one
        agent cannot be replayed by another."""
        base = f"{self.client}:{self.actor.kind}:{self.actor.label}"
        return f"{base}:{self.agent_id}" if self.agent_id else base
