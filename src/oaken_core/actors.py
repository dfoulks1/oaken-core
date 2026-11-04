# SPDX-License-Identifier: Apache-2.0
"""Who caused something: the user, an external agent, or Oaken itself.

Jobs record the actor that enqueued them, and the audit log (prompt 12) will record the same
thing, so an agent's work is always attributable. Operations also use it to apply the rule that
only a *user* action marks mail as read.
"""

from dataclasses import dataclass
from typing import Literal

ActorKind = Literal["user", "agent", "system"]


@dataclass(frozen=True)
class Actor:
    kind: ActorKind
    label: str = ""  # e.g. an agent's name; never a secret


SYSTEM = Actor("system")
USER = Actor("user")
