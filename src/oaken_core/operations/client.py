# SPDX-License-Identifier: Apache-2.0
"""``CoreClient``: calls the registry directly, in process.

Adapted from oaken-mail ``core/operations/client.py`` (cf3b187): the model-call counter, savings
and workspace scope are gone, the audit row is a change-log row, and an MCP caller behind an agent
token must also have been granted the operation.


This is the one path every operation takes, so the rules cannot be skipped: the MCP exposure check,
input validation, the permission class, the confirm gate, the handler, then the audit row. The
HTTP layer wraps this later; the tests drive it as it is.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from oaken_core.agents import Agent
from oaken_core.errors import InvalidInput, NotExposed, NotFound, PermissionDenied, SendDisabled
from oaken_core.ids import AgentId
from oaken_core.logging import get_logger
from oaken_core.operations.caller import CallerContext
from oaken_core.operations.context import OpContext
from oaken_core.operations.exposure import ExposureStore
from oaken_core.operations.gate import (
    Binding,
    InMemoryProposals,
    Proposal,
    ProposalStore,
    hash_input,
)
from oaken_core.operations.registry import AnyOperation, OperationRegistry, Preview
from oaken_core.operations.services import Services

log = get_logger("oaken_core.operations")


@dataclass(frozen=True)
class CallResult:
    """Either the operation ran (``output``) or it needs confirming first (``proposal``)."""

    request_id: str
    output: BaseModel | None = None
    proposal: Proposal | None = None

    @property
    def executed(self) -> bool:
        return self.proposal is None


class CoreClient:
    def __init__(
        self,
        registry: OperationRegistry,
        services: Services,
        *,
        proposals: ProposalStore | None = None,
        exposure: ExposureStore | None = None,
    ) -> None:
        self.registry = registry
        self.svc = services
        cfg = services.settings.operations
        self.proposals: ProposalStore = proposals or InMemoryProposals(
            services.clock, cfg.confirm_ttl_seconds, cfg.max_pending_confirmations
        )
        self.exposure = exposure or ExposureStore(
            services.db, services.clock, services.changelog, cfg.lift_never, cfg.override_never
        )

    def context(self, caller: CallerContext) -> OpContext:
        return OpContext(
            svc=self.svc, caller=caller, registry=self.registry, exposure=self.exposure
        )

    async def visible(self, caller: CallerContext) -> list[AnyOperation]:
        """The operations this caller may call: all of them, except that an MCP caller sees only
        what is allowed by code and turned on by the user."""
        if caller.client != "mcp":
            return self.registry.all()
        chosen = await self.exposure.settings()
        agent = await self._agent(caller)
        return [
            op for op in self.registry.all()
            if self.exposure.effective(op, chosen) and self._granted(op, agent)
        ]  # fmt: skip

    async def _agent(self, caller: CallerContext) -> Agent | None:
        """The agent behind an MCP call, read fresh (a revoke or a changed grant applies at once)."""
        if caller.agent_id is None or self.svc.agents is None:
            return None
        try:
            agent = await self.svc.agents.get(AgentId(caller.agent_id))
        except NotFound:
            raise PermissionDenied("invalid or revoked agent token") from None
        if agent.revoked:
            raise PermissionDenied("invalid or revoked agent token")
        return agent

    @staticmethod
    def _granted(op: AnyOperation, agent: Agent | None) -> bool:
        """An agent may call the job protocol, and what it was granted. The person's own MCP door
        (no agent) is bounded by the exposure settings alone."""
        if agent is None or op.agent_ok:
            return True
        return op.name in agent.operations or (
            op.plugin is not None and f"plugin:{op.plugin}" in agent.operations
        )

    async def call(
        self,
        name: str,
        payload: BaseModel | Mapping[str, Any] | None,
        caller: CallerContext,
        *,
        confirm: bool = False,
        confirmation_token: str | None = None,
    ) -> CallResult:
        """Run an operation.

        ``confirm=True`` is the CLI's or UI's way of saying the person asked for this exact action;
        it is ignored for an MCP caller, which can only confirm with the token from a proposal.
        A mutation that is not confirmed returns a ``Proposal`` and changes nothing.
        """
        op = self.registry.get(name)
        if caller.client == "mcp":
            # The same words whether it never exists for MCP, is switched off, or this agent was
            # not granted it: an agent learns nothing about what it was not offered.
            agent = await self._agent(caller)
            if not await self.exposure.enabled(op) or not self._granted(op, agent):
                raise NotExposed(f"operation {name!r} is not available to MCP clients")
        inp = self._parse(op, payload)
        if op.permission == "send":
            raise SendDisabled("sending is disabled in this version")
        ctx = self.context(caller)

        if op.permission == "mutate":
            binding = Binding(op.name, hash_input(op.name, inp), caller.key)
            if confirmation_token:
                self.proposals.redeem(confirmation_token, binding)
            elif not (confirm and caller.can_confirm):
                return CallResult(
                    caller.request_id, proposal=await self._propose(op, ctx, inp, binding)
                )

        output = await op.handler(ctx, inp)
        if not isinstance(output, op.output):
            output = op.output.model_validate(output)
        if op.audit == "always" or (op.permission == "mutate" and op.audit == "registry"):
            await self._audit(op, inp, caller)
        log.info(
            "operation ran", operation=op.name, client=caller.client, actor=caller.actor.kind,
            request_id=caller.request_id,
        )  # fmt: skip
        return CallResult(caller.request_id, output=output)

    @staticmethod
    def _parse(op: AnyOperation, payload: BaseModel | Mapping[str, Any] | None) -> BaseModel:
        try:
            data = payload.model_dump() if isinstance(payload, BaseModel) else dict(payload or {})
            parsed: BaseModel = op.input.model_validate(data)
            return parsed
        except ValidationError as exc:
            # Where and what, never the value: an input can hold a search the user typed.
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}"
                for e in exc.errors(include_input=False, include_url=False)[:5]
            )
            raise InvalidInput(f"{op.name}: {problems}") from None

    async def _propose(
        self, op: AnyOperation, ctx: OpContext, inp: BaseModel, binding: Binding
    ) -> Proposal:
        preview = await op.preview(ctx, inp) if op.preview else Preview(summary=op.summary)
        token, expires = self.proposals.issue(binding)
        shown = self.svc.settings.operations.proposal_changes_shown
        return Proposal(
            operation=op.name,
            summary=preview.summary,
            changes=preview.changes[:shown],
            total_changes=max(preview.total_changes, len(preview.changes)),
            confirmation_token=token,
            expires_at=expires,
        )

    async def _audit(self, op: AnyOperation, inp: BaseModel, caller: CallerContext) -> None:
        """The registry's own row: who ran what, through which door. It holds a hash of the input
        and the request id, never the input (a search can hold what the user typed)."""
        async with self.svc.db.transaction() as tx:
            await self.svc.changelog.append(
                tx, actor=caller.actor, subject_id=caller.request_id, key=f"call:{op.name}", op="call",
                new=json.dumps({"client": caller.client, "input_sha256": hash_input(op.name, inp),
                                **({"agent_id": caller.agent_id} if caller.agent_id else {})}),
            )  # fmt: skip
