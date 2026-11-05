# SPDX-License-Identifier: Apache-2.0
"""A local Ollama server as an Oaken Core embedder and LLM backend.

Lifted from oaken-mail ``oaken_ollama`` (cf3b187). Registered in the ``oaken_core.embedders`` entry-point
group. Core never imports it by name; it is chosen with ``[plugins.embedder.<name>]``, where ``plugin =
"ollama"`` and ``locality`` (unset means hosted, the strictest) say who may send it what.
"""

from oaken_core.ollama.client import OllamaConfig, is_loopback
from oaken_core.ollama.embedder import OllamaEmbedder
from oaken_core.ollama.llm import OllamaLLM, OllamaRemoteLLM

__all__ = ["OllamaConfig", "OllamaEmbedder", "OllamaLLM", "OllamaRemoteLLM", "is_loopback"]
