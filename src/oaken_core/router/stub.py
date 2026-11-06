# SPDX-License-Identifier: Apache-2.0
"""Stand-ins for tests and for trying core with no model: a scripted backend, a hashing embedder
and an in-memory vector store. Real backends live in plugins (``oaken-plugins``)."""

import json
import math
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from oaken_core.plugins.base import BasePlugin
from oaken_core.plugins.contracts import Health, Hit, LLMRequest, LLMResponse, VectorItem


class StubBackend(BasePlugin):
    """Answers with ``reply`` (a dict, or a function of the request and the model asked for). Records
    every call. ``up = False`` makes ``generate`` raise, as a dead host would."""

    version: ClassVar[str] = "1"

    def __init__(self, reply: dict[str, Any] | Callable[[LLMRequest, str], dict[str, Any]] | None = None,
                 *, model_id: str = "stub", context_window: int = 8192) -> None:  # fmt: skip
        self.reply = reply if reply is not None else {}
        self.model_id, self.context_window = model_id, context_window
        self.calls: list[tuple[LLMRequest, str]] = []
        self.up = True

    async def generate(self, request: LLMRequest, *, model: str | None = None) -> LLMResponse:
        if not self.up:
            raise ConnectionError("stub backend is down")
        run = model or self.model_id
        self.calls.append((request, run))
        data = self.reply(request, run) if callable(self.reply) else self.reply
        text = json.dumps(data)
        return LLMResponse(text=text, data=data, input_tokens=100, output_tokens=10, model_id=run)

    async def health(self) -> Health:
        return Health(self.up)


class StubEmbedder(BasePlugin):
    """A bag-of-words hash into ``dimensions`` buckets: texts that share words are close."""

    version: ClassVar[str] = "1"

    def __init__(self, dimensions: int = 32, model_id: str = "stub-embed") -> None:
        self.dimensions, self.model_id = dimensions, model_id
        self.embedded = 0

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded += len(texts)
        out = []
        for text in texts:
            v = [0.0] * self.dimensions
            for word in text.lower().split():
                v[sum(map(ord, word)) % self.dimensions] += 1.0
            out.append(v)
        return out


class MemoryVectors(BasePlugin):
    """Cosine similarity over lists. Fine for tests; a real store indexes."""

    version: ClassVar[str] = "1"

    def __init__(self) -> None:
        self.items: dict[str, dict[str, tuple[float, ...]]] = {}

    async def upsert(self, collection: str, items: Sequence[VectorItem]) -> None:
        self.items.setdefault(collection, {}).update({i.key: i.vector for i in items})

    async def delete(self, collection: str, keys: Sequence[str]) -> None:
        for k in keys:
            self.items.get(collection, {}).pop(k, None)

    async def query(self, collection: str, vector: Sequence[float], k: int) -> list[Hit]:
        hits = [Hit(key, _cosine(vector, v)) for key, v in self.items.get(collection, {}).items()]
        return sorted(hits, key=lambda h: (-h.score, h.key))[:k]

    async def stats(self) -> dict[str, int]:
        return {c: len(v) for c, v in self.items.items()}


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb) if na and nb else 0.0
