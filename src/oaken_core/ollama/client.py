# SPDX-License-Identifier: Apache-2.0
"""A small, careful client for Ollama's HTTP API, shared by the embedder and the LLM backend.

Careful means: it talks only to a loopback address unless the user said otherwise
(the router decides who may see what by ``locality``), it retries only what is safe to retry, it never
auto-pulls a model, and no error message carries the body of a request or a response (those hold the
owner's text).
"""

import asyncio
import ipaddress
from typing import Any
from urllib.parse import urlsplit

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from oaken_core.errors import AIBackendError, PluginError
from oaken_core.logging import get_logger

log = get_logger("oaken_core.ollama")

DEFAULT_URL = "http://127.0.0.1:11434"


class OllamaConfig(BaseModel):
    """``[plugins.<kind>.<name>]``. Nothing here is a secret."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = DEFAULT_URL
    model: str = ""  # the model to run; the plugin will not start without one
    keep_alive: str = "5m"  # how long Ollama keeps the model loaded after a call
    timeout: float = Field(default=120.0, gt=0)  # seconds, per request
    max_retries: int = Field(default=2, ge=0, le=10)  # extra tries after a connection error / 5xx
    backoff: float = Field(default=0.5, ge=0)  # seconds, doubled each retry
    batch_size: int = Field(default=32, ge=1, le=1024)  # texts per /api/embed call
    # Override whether a reasoning model thinks before it answers. Unset, each request decides, and
    # Oaken's tasks ask for no reasoning (a short JSON answer; reasoning costs time and can use up
    # the output limit before the answer is written). true turns it on for every call.
    think: bool | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any], who: str) -> "OllamaConfig":
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            fields = ", ".join(str(e["loc"][0]) for e in exc.errors() if e["loc"])
            raise PluginError(f"{who}: invalid [plugins.{who}] settings ({fields})") from None


def is_loopback(url: str) -> bool:
    """True for ``localhost``, ``127.0.0.0/8`` and ``::1`` and nothing else (no DNS lookups)."""
    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def check_url(url: str, who: str) -> bool:
    """Refuse a URL that is not http(s); return whether the host is loopback. Who may send what to a
    non-loopback host is the router's call (``locality``), not the plugin's."""
    if urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).hostname:
        raise PluginError(f"{who}: base_url must be an http(s) URL, got {url!r}")
    return is_loopback(url)


def _has_model(installed: list[str], wanted: str) -> bool:
    names = set(installed)
    return wanted in names or (":" not in wanted and f"{wanted}:latest" in names)


class OllamaClient:
    def __init__(
        self,
        config: OllamaConfig,
        who: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.config, self.who = config, who
        self._http = httpx2.AsyncClient(
            base_url=config.base_url,
            timeout=httpx2.Timeout(config.timeout, connect=10.0),
            transport=transport,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        tries = self.config.max_retries + 1
        for attempt in range(tries):
            last = attempt == tries - 1
            try:
                resp = await self._http.request(method, path, json=body)
            except (httpx2.ConnectError, httpx2.ConnectTimeout):
                if last:
                    raise AIBackendError(
                        f"{self.who}: cannot reach Ollama at {self.config.base_url}; is it running?"
                    ) from None
            except httpx2.TransportError as exc:
                raise AIBackendError(f"{self.who}: request failed ({type(exc).__name__})") from None
            else:
                if resp.status_code >= 500:
                    if last:
                        raise AIBackendError(f"{self.who}: Ollama answered {resp.status_code}")
                elif resp.status_code >= 400:
                    raise AIBackendError(f"{self.who}: Ollama answered {resp.status_code}")
                else:
                    try:
                        return resp.json()
                    except ValueError:
                        raise AIBackendError(f"{self.who}: Ollama sent invalid JSON") from None
            await asyncio.sleep(self.config.backoff * 2**attempt)
        raise AssertionError("unreachable")

    # -- endpoints -----------------------------------------------------------------------------
    async def installed_models(self) -> list[str]:
        """The model ids Ollama has pulled, from ``/api/tags``. Never pulls."""
        tags = await self._call("GET", "/api/tags")
        return [
            m.get("name", "")
            for m in tags.get("models", [])
            if isinstance(m, dict) and m.get("name")
        ]

    async def require_model(self, name: str) -> None:
        """Fail with the exact command to run when ``name`` is not installed. Never pulls."""
        installed = await self.installed_models()
        if not _has_model(installed, name):
            raise PluginError(
                f"{self.who}: model {name!r} is not installed in Ollama. Run: ollama pull {name}"
            )

    async def context_length(self, name: str) -> int:
        info = await self._call("POST", "/api/show", {"model": name})
        for key, value in (info.get("model_info") or {}).items():
            if key.endswith(".context_length") and isinstance(value, int):
                return value
        return 0

    async def embed(self, name: str, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.config.batch_size):
            batch = texts[i : i + self.config.batch_size]
            data = await self._call(
                "POST",
                "/api/embed",
                {"model": name, "input": batch, "keep_alive": self.config.keep_alive},
            )
            vectors = data.get("embeddings")
            if not isinstance(vectors, list) or len(vectors) != len(batch):
                raise AIBackendError(f"{self.who}: Ollama returned the wrong number of vectors")
            out.extend([float(x) for x in v] for v in vectors)
        return out

    async def chat(
        self,
        name: str,
        system: str,
        user: str,
        *,
        schema: dict[str, Any] | None,
        temperature: float,
        max_tokens: int | None,
        think: bool | None = None,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {"temperature": temperature}
        if max_tokens:
            options["num_predict"] = max_tokens
        body: dict[str, Any] = {
            "model": name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "keep_alive": self.config.keep_alive,
            "options": options,
        }
        if schema is not None:
            body["format"] = schema
        if think is not None:  # None: leave it to the model (a backend skips what it cannot do)
            body["think"] = think
        data = await self._call("POST", "/api/chat", body)
        if not isinstance(data, dict) or not isinstance(data.get("message"), dict):
            raise AIBackendError(f"{self.who}: Ollama sent an unexpected reply")
        # What we asked, beside the answer, for the trace (the messages are the caller's own).
        data["_sent"] = {
            "options": options,
            "think": body.get("think"),
            "format": "schema" if schema is not None else None,
            "keep_alive": body["keep_alive"],
        }
        return data

    async def reachable(self) -> bool:
        try:
            await self._call("GET", "/api/tags")
        except AIBackendError:
            return False
        return True
