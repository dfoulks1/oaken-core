# SPDX-License-Identifier: Apache-2.0
"""``OllamaEmbedder``: turns text into vectors with a model served by Ollama."""

from collections.abc import Sequence

import httpx2

from oaken_core.errors import PluginError
from oaken_core.ollama.client import OllamaClient, OllamaConfig, check_url
from oaken_core.plugins.base import BaseEmbedder
from oaken_core.plugins.contracts import Health, PluginContext


class OllamaEmbedder(BaseEmbedder):
    """Registered as ``oaken_core.embedders: ollama``. The model is the instance's ``model`` setting.
    ``start`` confirms the model is installed, reads its context length and embeds one probe text to
    learn the vector size."""

    config_model = OllamaConfig
    requires_config = ("model",)

    def __init__(self, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._client: OllamaClient | None = None

    async def start(self, ctx: PluginContext) -> None:
        await super().start(ctx)
        cfg = self.config  # parsed by BasePlugin from config_model
        check_url(cfg.base_url, ctx.name)
        self.model_id = cfg.model
        self._client = OllamaClient(cfg, ctx.name, transport=self._transport)
        try:
            await self._client.require_model(self.model_id)
            window = await self._client.context_length(self.model_id)
            if window:
                self.max_input_tokens = window
            self.dimensions = len((await self._client.embed(self.model_id, ["dimension probe"]))[0])
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        client = self._client
        if client is None:
            raise PluginError("start() was not called")
        if not texts:
            return []
        return await client.embed(self.model_id, list(texts))

    async def health(self) -> Health:
        if self._client and await self._client.reachable():
            return Health(True)
        return Health(False, "Ollama is not reachable")
