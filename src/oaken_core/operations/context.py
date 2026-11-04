# SPDX-License-Identifier: Apache-2.0
"""What a handler receives besides its input.

Trimmed from oaken-mail ``core/operations/context.py``: the mail ``Services`` bag and ``Limits`` are
gone. ``svc`` is the ``Services`` the daemon built.
"""

from dataclasses import dataclass
from typing import Any

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
