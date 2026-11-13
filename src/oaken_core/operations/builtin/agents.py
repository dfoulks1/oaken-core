# SPDX-License-Identifier: Apache-2.0
"""Agents: the tokens MCP clients and external workers authenticate with. All human-only
(``mcp="never"``): an agent can never mint or widen a token, its own or another's."""

from pydantic import BaseModel, Field

from oaken_core.errors import InvalidInput, NotFound
from oaken_core.ids import AgentId
from oaken_core.operations.context import OpContext
from oaken_core.operations.registry import Change, Preview, operation
from oaken_core.operations.services import need


class AgentRow(BaseModel):
    id: str
    label: str
    kinds: list[str]
    queues: list[str] | None
    operations: list[str]
    max_leases: int
    max_per_hour: int
    revoked: bool
    last_seen_at: str | None


def _row(a) -> AgentRow:  # type: ignore[no-untyped-def]
    return AgentRow(id=a.id, label=a.label, kinds=sorted(a.kinds),
                    queues=sorted(a.queues) if a.queues is not None else None,
                    operations=sorted(a.operations), max_leases=a.max_leases,
                    max_per_hour=a.max_per_hour, revoked=a.revoked,
                    last_seen_at=a.last_seen_at.isoformat() if a.last_seen_at else None)  # fmt: skip


class AgentCreateIn(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    kinds: list[str] = Field(default_factory=list, description="Job kinds it may lease (opted in under [swarm]); none: the jobs in its queues whose result core can act on (an enrich stage's).")
    queues: list[str] | None = Field(default=None, description="Queues it may lease from; default all.")
    operations: list[str] = Field(default_factory=list, description="Operations it may call beyond the job protocol.")
    max_leases: int | None = Field(default=None, ge=1)
    max_per_hour: int | None = Field(default=None, ge=1)


class AgentCreateOut(BaseModel):
    agent: AgentRow
    token: str = Field(description="Shown once. Only its hash is kept.")


def _check_grants(ctx: OpContext, names: list[str]) -> None:
    """A grant names real operations that MCP may ever reach (``never`` stays above it)."""
    reg, exp = ctx.registry, ctx.exposure
    for n in names:
        if n.startswith("plugin:"):
            if not any(op.plugin == n[7:] for op in reg.all()):
                raise InvalidInput(f"no plugin named {n[7:]!r}")
            continue
        try:
            op = reg.get(n)
        except NotFound:
            raise InvalidInput(f"no operation named {n!r}") from None
        if exp is not None and not exp.allowed(op):
            raise InvalidInput(f"{n} is never offered over MCP; no grant can change that")


async def preview_create(ctx: OpContext, inp: AgentCreateIn) -> Preview:
    return Preview(summary=f"Create agent {inp.label!r} for {', '.join(inp.kinds)}",
                   changes=[Change(target=inp.label, field="agent", after="new")], total_changes=1)  # fmt: skip


@operation("agent_create", "Create an agent token. The token is shown once.", permission="mutate",
           input=AgentCreateIn, output=AgentCreateOut, mcp="never", group="agent", preview=preview_create)  # fmt: skip
async def agent_create(ctx: OpContext, inp: AgentCreateIn) -> AgentCreateOut:
    _check_grants(ctx, inp.operations)
    agent, token = await need(ctx.svc.agents, "the agent registry").create(
        inp.label, inp.kinds, queues=inp.queues, operations=inp.operations,
        max_leases=inp.max_leases, max_per_hour=inp.max_per_hour,
    )  # fmt: skip
    return AgentCreateOut(agent=_row(agent), token=token)


class AgentListIn(BaseModel):
    include_revoked: bool = False


class AgentListOut(BaseModel):
    agents: list[AgentRow]
    queues: list[dict[str, str]] = Field(default_factory=list, description="The queues, for [swarm] allowed_queues: name, label, kind.")


@operation("agent_list", "The agents and what each may do. Tokens are never shown.", permission="read",
           input=AgentListIn, output=AgentListOut, mcp="never", group="agent")  # fmt: skip
async def agent_list(ctx: OpContext, inp: AgentListIn) -> AgentListOut:
    found = await need(ctx.svc.agents, "the agent registry").list(include_revoked=inp.include_revoked)
    return AgentListOut(agents=[_row(a) for a in found], queues=[{"name": q.name, "label": q.label or q.name, "kind": q.kind} for q in ctx.svc.queues.all() if not q.hidden])


class AgentRef(BaseModel):
    agent_id: str


class AgentGrantIn(AgentRef):
    operations: list[str] = Field(description="Replaces the agent's grants.")


async def preview_grant(ctx: OpContext, inp: AgentGrantIn) -> Preview:
    a = await need(ctx.svc.agents, "the agent registry").get(AgentId(inp.agent_id))
    return Preview(summary=f"Set what {a.label!r} may call",
                   changes=[Change(target=a.id, field="operations", before=sorted(a.operations), after=sorted(set(inp.operations)))],
                   total_changes=1)  # fmt: skip


@operation("agent_grant", "Replace the operations an agent may call beyond the job protocol.",
           permission="mutate", input=AgentGrantIn, output=AgentRow, mcp="never", group="agent",
           preview=preview_grant)  # fmt: skip
async def agent_grant(ctx: OpContext, inp: AgentGrantIn) -> AgentRow:
    _check_grants(ctx, inp.operations)
    return _row(await need(ctx.svc.agents, "the agent registry").grant(AgentId(inp.agent_id), inp.operations))


class RevokeOut(BaseModel):
    revoked: bool


async def preview_revoke(ctx: OpContext, inp: AgentRef) -> Preview:
    a = await need(ctx.svc.agents, "the agent registry").get(AgentId(inp.agent_id))
    return Preview(summary=f"Revoke agent {a.label!r}; jobs it holds go back to the queue",
                   changes=[Change(target=a.id, field="revoked", before=a.revoked, after=True)], total_changes=1)  # fmt: skip


@operation("agent_revoke", "Revoke an agent at once.", permission="mutate", input=AgentRef,
           output=RevokeOut, mcp="never", group="agent", preview=preview_revoke)  # fmt: skip
async def agent_revoke(ctx: OpContext, inp: AgentRef) -> RevokeOut:
    return RevokeOut(revoked=await need(ctx.svc.agents, "the agent registry").revoke(AgentId(inp.agent_id)))


OPERATIONS = (agent_create, agent_list, agent_grant, agent_revoke)
