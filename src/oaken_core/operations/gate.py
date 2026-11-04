# SPDX-License-Identifier: Apache-2.0
"""The confirm gate's two-phase flow, for a mutation that is not confirmed by a person.

First call: no side effects. The caller gets a ``Proposal`` (what would change, with ids and
before/after) and a short-lived, single-use ``confirmation_token``. Second call: presenting the
token runs the operation. A token is bound to the operation, the exact validated input (by hash)
and the caller (class and actor), so it cannot be replayed, altered, or lent to another agent.

Tokens live in memory only. A restart forgets them, which is the safe direction: the caller asks
again. The store is behind an interface so a transport can swap it.
"""

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import BaseModel

from oaken_core.clock import Clock
from oaken_core.errors import ConfirmationInvalid, QuotaExceeded
from oaken_core.operations.registry import Change

TOKEN_PREFIX = "oaken_confirm_"  # noqa: S105 (a prefix, not a secret)


class Proposal(BaseModel):
    """The answer to a mutation that still needs confirming. Nothing has happened."""

    operation: str
    summary: str
    changes: list[Change]
    total_changes: int
    confirmation_token: str
    expires_at: datetime
    how_to_confirm: str = "Call the same operation again with the same input and this token."


@dataclass(frozen=True)
class Binding:
    operation: str
    input_sha256: str
    caller_key: str


def hash_input(operation: str, value: BaseModel) -> str:
    """A hash of the validated input (defaults applied, keys sorted), so two spellings of one call
    are the same call and any change to it is a different one."""
    canonical = json.dumps(
        {"op": operation, "input": value.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class ProposalStore(Protocol):
    def issue(self, binding: Binding) -> tuple[str, datetime]:
        """A new token for ``binding`` and when it expires."""
        ...

    def redeem(self, token: str, binding: Binding) -> None:
        """Use the token. Raises ``ConfirmationInvalid`` unless it is live and for exactly this
        call. A token is spent by any attempt that reaches it, right or wrong."""
        ...


@dataclass
class _Entry:
    binding: Binding
    expires: datetime


class InMemoryProposals:
    def __init__(self, clock: Clock, ttl_seconds: float, max_per_caller: int) -> None:
        self.clock = clock
        self.ttl = timedelta(seconds=ttl_seconds)
        self.max_per_caller = max_per_caller
        self._live: dict[str, _Entry] = {}  # sha256(token) -> entry

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _purge(self) -> None:
        now = self.clock.now()
        for k in [k for k, e in self._live.items() if e.expires <= now]:
            del self._live[k]

    def pending(self, caller_key: str) -> int:
        self._purge()
        return sum(e.binding.caller_key == caller_key for e in self._live.values())

    def issue(self, binding: Binding) -> tuple[str, datetime]:
        if self.pending(binding.caller_key) >= self.max_per_caller:
            raise QuotaExceeded(
                "too many proposals are waiting to be confirmed; confirm or let them expire"
            )
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        expires = self.clock.now() + self.ttl
        self._live[self._key(token)] = _Entry(binding, expires)
        return token, expires

    def redeem(self, token: str, binding: Binding) -> None:
        self._purge()
        entry = self._live.pop(self._key(token), None)  # spent whatever happens next
        if entry is None:
            raise ConfirmationInvalid("the confirmation token is unknown, used already or expired")
        if entry.binding != binding:
            raise ConfirmationInvalid("the confirmation token is for a different call")
