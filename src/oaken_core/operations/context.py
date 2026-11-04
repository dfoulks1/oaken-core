# SPDX-License-Identifier: Apache-2.0
"""What a handler receives besides its input.

Trimmed from oaken-mail ``core/operations/context.py``: the mail ``Services`` bag is gone.
``svc`` is whatever the daemon built (step 4 gives it a generic shape).
"""

from dataclasses import dataclass
from typing import Any

from oaken_core.operations.caller import CallerContext


@dataclass(frozen=True)
class OpContext:
    """``caller`` says who and through which door. By the time a mutating handler runs, the gate
    has approved the call: a handler never decides that for itself, and never trusts a flag that
    came in with the input."""

    svc: Any
    caller: CallerContext
