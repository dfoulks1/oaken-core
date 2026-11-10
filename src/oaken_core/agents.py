# SPDX-License-Identifier: Apache-2.0
"""External agents: separately authenticated workers that lease jobs .

An agent is a row in ``raw.db`` with its own token, the job kinds it may lease, optionally the
queues it may lease from, and two quotas. Only the SHA-256 of the token is stored, and the token is
shown once, at creation. Tokens are 256 random bits, so a plain hash is enough (there is nothing
to brute-force). The queue enforces scope and quotas inside the claim transaction
(``Jobs.claim(agent=...)``), so a revoked or over-quota agent can never slip a job through.
"""

import hashlib
import json
import secrets
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from sqlite3 import Row

from oaken_core.clock import Clock, fmt_ts, parse_ts
from oaken_core.db import Database
from oaken_core.errors import ConfigError, NotFound, PermissionDenied, QuotaExceeded
from oaken_core.events import EventBus, JobStateChanged
from oaken_core.ids import AgentId, JobId, new_agent_id
from oaken_core.settings import SwarmSettings

TOKEN_PREFIX = "oaken_agent_"  # noqa: S105 (a prefix, not a secret)
MAX_LABEL_CHARS = 64


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Agent:
    id: AgentId
    label: str
    kinds: frozenset[str]
    queues: frozenset[str] | None  # None: every queue
    operations: frozenset[str]  # granted beyond the job protocol: names and ``plugin:<name>``
    max_leases: int
    max_per_hour: int
    created_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None

    @property
    def worker(self) -> str:
        """The ``locked_by`` value for this agent's leases."""
        return f"agent:{self.id}"

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    @classmethod
    def from_row(cls, r: Row) -> "Agent":
        queues = json.loads(r["queues_json"]) if r["queues_json"] else None
        return cls(
            id=AgentId(r["id"]),
            label=r["label"],
            kinds=frozenset(json.loads(r["kinds_json"])),
            queues=frozenset(queues) if queues is not None else None,
            operations=frozenset(json.loads(r["operations_json"] or "[]")),
            max_leases=r["max_leases"],
            max_per_hour=r["max_per_hour"],
            created_at=parse_ts(r["created_at"]),
            last_seen_at=parse_ts(r["last_seen_at"]) if r["last_seen_at"] else None,
            revoked_at=parse_ts(r["revoked_at"]) if r["revoked_at"] else None,
        )


class AgentRegistry:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        settings: SwarmSettings | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings or SwarmSettings()
        self.bus = bus

    async def create(
        self,
        label: str,
        kinds: Collection[str] = (),
        *,
        queues: Collection[str] | None = None,
        operations: Collection[str] = (),
        max_leases: int | None = None,
        max_per_hour: int | None = None,
    ) -> tuple[Agent, str]:
        """Register an agent. Returns it with its token, which cannot be shown again."""
        s = self.settings
        if not s.enabled:
            raise PermissionDenied("the swarm is disabled ([swarm] enabled = false)")
        label = label.strip()
        if not label or len(label) > MAX_LABEL_CHARS:
            raise ConfigError(f"an agent label is 1 to {MAX_LABEL_CHARS} characters")
        wanted = sorted(set(kinds))
        if not wanted and not queues:
            raise ConfigError("an agent needs at least one queue or job kind")  # no kinds: any job in its queues that takes a result
        refused = [k for k in wanted if k not in s.allowed_kinds]
        if refused:
            raise ConfigError(
                f"job kinds not allowed for agents: {', '.join(refused)}"
                " (add them to [swarm] allowed_kinds to opt in)"
            )
        closed = [q for q in sorted(set(queues or ())) if q not in s.allowed_queues]
        if closed:
            raise ConfigError(
                f"queues not allowed for agents: {', '.join(closed)}"
                " (add them to [swarm] allowed_queues to opt in)"
            )
        leases = max_leases or s.default_max_leases
        hourly = max_per_hour or s.default_max_per_hour
        if leases < 1 or hourly < 1:
            raise ConfigError("quotas must be at least 1")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        now = self.clock.now()
        agent_id = new_agent_id()
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT COUNT(*) AS n FROM agents WHERE revoked_at IS NULL")
            if row and row["n"] >= s.max_agents:
                raise QuotaExceeded(f"at most {s.max_agents} active agents ([swarm] max_agents)")
            await tx.execute(
                "INSERT INTO agents (id, label, token_hash, kinds_json, queues_json,"
                " max_leases, max_per_hour, created_at, operations_json)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    agent_id,
                    label,
                    hash_token(token),
                    json.dumps(wanted),
                    json.dumps(sorted(queues)) if queues is not None else None,
                    leases,
                    hourly,
                    fmt_ts(now),
                    json.dumps(sorted(set(operations))),
                ),
            )
            created = await tx.fetchone("SELECT * FROM agents WHERE id = ?", (agent_id,))
        assert created is not None  # noqa: S101 (inserted in this transaction)
        return Agent.from_row(created), token

    async def authenticate(self, token: str) -> Agent:
        """The agent for ``token``, or ``PermissionDenied`` (unknown, revoked or swarm off)."""
        row = await self.db.fetchone(
            "SELECT * FROM agents WHERE token_hash = ?", (hash_token(token),)
        )
        agent = Agent.from_row(row) if row else None
        if agent is None or agent.revoked or not self.settings.enabled:
            raise PermissionDenied("invalid or revoked agent token")
        return agent

    async def get(self, agent_id: AgentId) -> Agent:
        row = await self.db.fetchone("SELECT * FROM agents WHERE id = ?", (agent_id,))
        if row is None:
            raise NotFound(f"no such agent: {agent_id}")
        return Agent.from_row(row)

    async def list(self, *, include_revoked: bool = False) -> list[Agent]:
        where = "" if include_revoked else " WHERE revoked_at IS NULL"
        rows = await self.db.fetchall(f"SELECT * FROM agents{where} ORDER BY created_at, id")  # noqa: S608
        return [Agent.from_row(r) for r in rows]

    async def grant(self, agent_id: AgentId, operations: Collection[str]) -> Agent:
        """Replace the operations an agent may call beyond the job protocol. Whether a name is real
        or allowed over MCP is the caller's check (the ``mcp="never"`` ceiling stays above this)."""
        async with self.db.transaction() as tx:
            changed = await tx.execute(
                "UPDATE agents SET operations_json = ? WHERE id = ? AND revoked_at IS NULL",
                (json.dumps(sorted(set(operations))), agent_id),
            )
            if not changed:
                raise NotFound(f"no such active agent: {agent_id}")
        return await self.get(agent_id)

    async def revoke(self, agent_id: AgentId) -> bool:
        """Revoke an agent at once. Jobs it holds go back to the queue (the attempt is not
        counted) and its later heartbeats and results are refused, because it no longer holds
        the lease. Returns False if it was already revoked."""
        now = self.clock.now()
        stamp = fmt_ts(now)
        released: list[tuple[JobId, str, str, str | None]] = []
        async with self.db.transaction() as tx:
            if not await tx.fetchone("SELECT 1 FROM agents WHERE id = ?", (agent_id,)):
                raise NotFound(f"no such agent: {agent_id}")
            changed = await tx.execute(
                "UPDATE agents SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (stamp, agent_id),
            )
            if not changed:
                return False
            held = await tx.fetchall(
                "SELECT id, kind, queue, subject FROM jobs WHERE status = 'running' AND agent_id = ?",
                (agent_id,),
            )
            for r in held:
                await tx.execute(
                    "UPDATE jobs SET status = 'queued', locked_by = NULL, locked_until = NULL,"
                    " attempts = attempts - 1, run_after = ?, updated_at = ? WHERE id = ?",
                    (stamp, stamp, r["id"]),
                )
                released.append((JobId(r["id"]), r["kind"], r["queue"], r["subject"]))
        if self.bus:
            for job_id, kind, queue, subject in released:
                await self.bus.publish(
                    JobStateChanged(
                        ts=now,
                        job_id=job_id,
                        kind=kind,
                        queue=queue,
                        status="queued",
                        subject=subject,
                    )
                )
        return True
