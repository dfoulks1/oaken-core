# SPDX-License-Identifier: Apache-2.0
"""A pretend Ollama server for the tests: the real API shapes, answered in memory.

Lifted from oaken-mail ``oaken_ollama/tests/conftest.py`` (cf3b187). The fixtures in
``fixtures/ollama`` are written from Ollama's API documentation, not recorded from a running server.
Nothing here touches the network and no model is ever downloaded.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2

FIXTURES = Path(__file__).parent / "fixtures" / "ollama"
DIMS = 8


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


def vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [b / 255 for b in digest[:DIMS]]


@dataclass
class FakeOllama:
    """``transport`` goes to the plugin. ``calls`` records ``(method, path, body)``; ``fail`` holds
    responses (a status code or an exception) to hand out before behaving normally."""

    tags: dict[str, Any] = field(default_factory=lambda: fixture("tags.json"))
    calls: list[tuple[str, str, Any]] = field(default_factory=list)
    fail: list[int | Exception] = field(default_factory=list)
    chat_reply: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def paths(self) -> list[str]:
        return [p for _, p, _ in self.calls]

    def bodies(self, path: str) -> list[Any]:
        return [b for _, p, b in self.calls if p == path]

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, request.url.path, body))
        if self.fail:
            nxt = self.fail.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return httpx2.Response(nxt, json={"error": "boom"})
        path = request.url.path
        if path == "/api/tags":
            return httpx2.Response(200, json=self.tags)
        if path == "/api/show":
            return httpx2.Response(200, json=fixture("show_embed.json" if "embed" in body["model"] else "show_llm.json"))
        if path == "/api/embed":
            return httpx2.Response(200, json={"embeddings": [vector(t) for t in body["input"]]})
        if path == "/api/chat":
            if self.chat_reply:
                return httpx2.Response(200, json=self.chat_reply(body))
            return httpx2.Response(200, json=fixture("chat_json.json" if "format" in body else "chat_text.json"))
        return httpx2.Response(404, json={"error": "not found"})
