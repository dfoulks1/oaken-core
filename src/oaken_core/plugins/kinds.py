# SPDX-License-Identifier: Apache-2.0
"""Plugin kinds. Not a closed list: a product registers its own with ``KindRegistry.register``.

A kind names its entry-point group, the members a plugin of that kind must have, and how the host
treats it: ``first`` kinds start before everything else (the store), ``exclusive`` kinds have one
active plugin (a store, an auth), and ``analyzer`` kinds take part in the ``needs`` graph.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass

from oaken_core.errors import ConfigError, PluginError

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


@dataclass(frozen=True)
class KindSpec:
    name: str
    group: str  # the Python entry-point group
    required: tuple[str, ...] = ()
    analyzer: bool = False
    exclusive: bool = False
    first: bool = False


class KindRegistry:
    def __init__(self, specs: tuple[KindSpec, ...] = ()) -> None:
        self._kinds: dict[str, KindSpec] = {}
        for spec in specs:
            self.register(spec)

    @classmethod
    def shipped(cls) -> "KindRegistry":
        """The kinds core ships. A product adds more with ``register``."""
        return cls(SHIPPED)

    def register(self, spec: KindSpec) -> KindSpec:
        if not _NAME.match(spec.name):
            raise ConfigError(f"kind name {spec.name!r} must match {_NAME.pattern}")
        if spec.name in self._kinds:
            raise ConfigError(f"kind {spec.name!r} is already registered")
        if any(k.group == spec.group for k in self._kinds.values()):
            raise ConfigError(f"entry-point group {spec.group!r} is already used")
        self._kinds[spec.name] = spec
        return spec

    def get(self, name: str) -> KindSpec:
        try:
            return self._kinds[name]
        except KeyError:
            raise PluginError(f"no plugin kind named {name!r}") from None

    def ordered(self) -> list[KindSpec]:
        """The kinds in load order: ``first`` kinds, then the rest in the order registered."""
        return sorted(self._kinds.values(), key=lambda k: not k.first)

    def __iter__(self) -> Iterator[KindSpec]:
        return iter(self._kinds.values())

    def __contains__(self, name: object) -> bool:
        return name in self._kinds


def check_queue_kind(kinds: "KindRegistry", queue: str, kind: str) -> None:
    """A queue is the stage of one analyzer kind: refuse a name core does not know, or a kind that is not
    an analyzer (the embedder, a store, a provider: there is one of those, or no stage to put it in)."""
    if kind not in kinds:
        raise ConfigError(f"[queues.{queue}] kind {kind!r} is not a plugin kind (known: {', '.join(k.name for k in kinds)})")
    if not kinds.get(kind).analyzer:
        stages = ", ".join(k.name for k in kinds if k.analyzer)
        raise ConfigError(f"[queues.{queue}] kind {kind!r} is not an analyzer, so it has no queue (analyzers: {stages})")


SHIPPED = (
    KindSpec("store", "oaken_core.stores", ("open",), exclusive=True, first=True),
    KindSpec("auth", "oaken_core.auth", ("authenticate", "authorize"), exclusive=True),
    KindSpec("gateway", "oaken_core.gateways", ("serve",)),
    KindSpec("provider", "oaken_core.providers"),
    KindSpec("llm_backend", "oaken_core.llm_backends", ("model_id", "context_window", "generate")),
    KindSpec("embedder", "oaken_core.embedders", ("model_id", "dimensions", "embed")),
    KindSpec("vector_store", "oaken_core.vector_stores", ("upsert", "delete", "query", "stats")),
    KindSpec("cache_store", "oaken_core.cache_stores", ("key", "get", "put", "stats", "clear")),
    KindSpec("triager", "oaken_core.triagers", ("version", "needs_llm", "rank", "triage"), analyzer=True),
    KindSpec("enricher", "oaken_core.enrichers", ("version", "needs_llm", "enrich"), analyzer=True),
    KindSpec("action", "oaken_core.actions", ("version", "needs_llm", "routes", "act"), analyzer=True),
    # Where an accepted intake item is stored. Core has no nouns, so a product supplies this one.
    KindSpec("ingestor", "oaken_core.ingestors", ("ingest",), exclusive=True),
)
