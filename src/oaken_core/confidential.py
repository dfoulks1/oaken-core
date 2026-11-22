# SPDX-License-Identifier: Apache-2.0
"""Confidentiality: the one place that says what is confidential and who may see it.

An item is confidential when its effective ``confidential`` label is ``"true"``. Something that
reads text leaves this machine or stays on it: a backend's or an agent's ``locality`` says which,
in the router's words (``local``, ``lan``, ``hosted``). Only ``local`` may see confidential text.
A ``lan`` backend may still be allowlisted by the router (``[router] lan_allowlist``); an agent has
no such list, because the person states its locality and core cannot check it.

Every check calls these helpers rather than comparing strings itself, so a stricter rule (for
example a missing label meaning confidential) changes in one place.
"""

from collections.abc import Mapping
from typing import Literal

LABEL = "confidential"
Locality = Literal["local", "lan", "hosted"]
LOCALITIES: tuple[Locality, ...] = ("local", "lan", "hosted")
STRICTEST: Locality = "hosted"  # an unknown or missing locality is treated as off the machine


def is_confidential(labels: Mapping[str, str]) -> bool:
    """Whether a subject with these (effective) labels is confidential."""
    return labels.get(LABEL) == "true"


def may_see_confidential(locality: str) -> bool:
    """Whether something at ``locality`` may be handed confidential text. Only ``local`` may."""
    return locality == "local"
