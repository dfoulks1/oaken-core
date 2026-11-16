# SPDX-License-Identifier: Apache-2.0
"""What a handler receives besides its input.

Trimmed from oaken-mail ``core/operations/context.py``: the mail ``Services`` bag and ``Limits`` are
gone. ``svc`` is the ``Services`` the daemon built.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from oaken_core.confidential import is_confidential
from oaken_core.errors import ConfidentialWithheld
from oaken_core.operations.caller import CallerContext


@dataclass(frozen=True)
class OpContext:
    """``caller`` says who and through which door. By the time a mutating handler runs, the gate
    has approved the call: a handler never decides that for itself, and never trusts a flag that
    came in with the input."""

    svc: Any  # oaken_core.operations.services.Services
    caller: CallerContext
    registry: Any = None  # the OperationRegistry, for operations that list operations
    exposure: Any = None  # the ExposureStore

    # -- the confidential gate: what a handler that reads items calls before it answers ----------
    async def withhold_confidential(self, subject_id: str) -> None:
        """Raise ``ConfidentialWithheld`` if the subject is labelled confidential and the caller may
        not see such items (an MCP client its door has not cleared). Call it before reading."""
        if not self.caller.may_see_confidential and is_confidential(await self.svc.labels.all(subject_id)):
            raise ConfidentialWithheld(f"{subject_id!r} is confidential, and this client may not see confidential items")

    async def confidential_among(self, subject_ids: Iterable[str]) -> set[str]:
        """The subjects in ``subject_ids`` this caller may not see: leave them out of a list. Empty
        for a caller cleared for confidential items."""
        if self.caller.may_see_confidential:
            return set()
        return {s for s in set(subject_ids) if is_confidential(await self.svc.labels.all(s))}
