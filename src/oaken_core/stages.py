# SPDX-License-Identifier: Apache-2.0
"""The contracts of the stages after triage: enrich and act.

Triage judges an item before it is stored (``intake.py``). These two run on a *stored* subject.

- An **enricher** derives data from the subject: ``enrich(target) -> Enrichment | None``. Core stores the
  result under the enricher's name, keyed by the subject's content, so a rerun is a cache hit. Earlier
  enrichers' results are in ``target.enrichments``.
- An **action** does work outside the store, for a route a triager set on its verdict (ADR 0019):
  ``act(target) -> ActionResult``. Core queues one job per (subject, action, route). An action must be
  idempotent on that key; core adds no undo.

Both targets carry everything an ``IntakeItem`` does, so ``item_text(target)`` works on either. A model
sees the target only as ``UntrustedText``, like any outside text.

shortcut: this is the contract only. The queue loops that call ``enrich`` and ``act`` are not built; until
they are, a plugin's own job or test is the caller.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

from oaken_core.intake import IntakeItem


@dataclass(frozen=True)
class Enrichment:
    """What an enricher adds. ``data`` must be JSON-serializable. ``labels`` are written to the subject."""

    data: dict[str, Any] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EnrichTarget(IntakeItem):
    """A stored subject, for an enricher or an action."""

    enrichments: dict[str, Enrichment] = field(
        default_factory=dict
    )  # what the enrichers before this one added, by name


@dataclass(frozen=True)
class ActionTarget(EnrichTarget):
    """A subject routed to an action. ``route`` is the name the triager set on its verdict."""

    route: str = ""


@dataclass(frozen=True)
class ActionResult:
    """``detail`` goes to the trace and the logger: say what happened, never quote the item."""

    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(
        default_factory=dict
    )  # what ``get_action`` may show beyond success or failure


class Enricher(Protocol):
    name: str

    async def enrich(self, target: EnrichTarget) -> Enrichment | None:
        """What to add, or None for nothing."""
        ...


class Action(Protocol):
    name: str
    routes: tuple[str, ...]  # the route names this action answers to

    async def act(self, target: ActionTarget) -> ActionResult: ...
