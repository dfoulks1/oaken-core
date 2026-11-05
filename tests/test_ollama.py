# SPDX-License-Identifier: Apache-2.0
"""The Ollama embedder against a pretend server, plus the embedder contract suite.

Lifted from oaken-mail ``oaken_ollama/tests/test_ollama.py`` (cf3b187), the embedder and client parts."""

from typing import Any

import httpx2
import pytest

from oaken_core.errors import AIBackendError, PluginError
from oaken_core.ollama import OllamaEmbedder, is_loopback
from oaken_core.testing.contracts import EmbedderContract, make_context, verify_embedder

from .ollama_fake import DIMS, FakeOllama

MODEL = {"model": "nomic-embed-text", "backoff": 0}


@pytest.fixture
def ollama() -> FakeOllama:
    return FakeOllama()


async def started(fake: FakeOllama, **config: Any) -> OllamaEmbedder:
    emb = OllamaEmbedder(transport=fake.transport)
    await emb.start(make_context("embedder", config={**MODEL, **config}))
    return emb


class TestOllamaEmbedder(EmbedderContract):
    @pytest.fixture
    def embedder(self, ollama: FakeOllama) -> OllamaEmbedder:
        return OllamaEmbedder(transport=ollama.transport)

    @pytest.fixture
    def embedder_config(self) -> dict[str, Any]:
        return MODEL


async def test_the_suite_passes_as_one_call(ollama: FakeOllama) -> None:
    await verify_embedder(lambda: OllamaEmbedder(transport=ollama.transport), MODEL)


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434", "http://localhost:11434", "http://127.9.9.9", "http://[::1]:11434"])
def test_loopback_addresses(url: str) -> None:
    assert is_loopback(url)


@pytest.mark.parametrize(
    "url",
    ["http://192.168.1.5:11434", "http://10.0.0.2", "http://ollama.example.com", "http://0.0.0.0:11434",
     "http://localhost.example.com", "http://127.0.0.1.example.com"],
)  # fmt: skip
def test_other_addresses_are_not_loopback(url: str) -> None:
    assert not is_loopback(url)


async def test_a_bad_url_scheme_or_unknown_setting_is_a_plugin_error(ollama: FakeOllama) -> None:
    with pytest.raises(PluginError, match="http"):
        await started(ollama, base_url="ftp://127.0.0.1")
    with pytest.raises(PluginError, match="bogus"):
        await started(ollama, bogus=1)


async def test_a_missing_model_says_exactly_what_to_run_and_does_not_pull(ollama: FakeOllama) -> None:
    ollama.tags = {"models": []}
    with pytest.raises(PluginError, match="ollama pull nomic-embed-text"):
        await started(ollama)
    assert all(p in {"/api/tags"} for p in ollama.paths())  # no /api/pull, nothing else


async def test_the_embedder_discovers_its_dimensions_and_context(ollama: FakeOllama) -> None:
    emb = await started(ollama)
    assert emb.model_id == "nomic-embed-text" and emb.dimensions == DIMS and emb.max_input_tokens == 2048
    assert ollama.paths()[:3] == ["/api/tags", "/api/show", "/api/embed"]


async def test_embeddings_are_sent_in_batches(ollama: FakeOllama) -> None:
    emb = await started(ollama, batch_size=2)
    ollama.calls.clear()
    assert len(await emb.embed([f"t{i}" for i in range(5)])) == 5
    assert [len(b["input"]) for b in ollama.bodies("/api/embed")] == [2, 2, 1]
    assert ollama.bodies("/api/embed")[0]["keep_alive"] == "5m"


async def test_keep_alive_is_configurable(ollama: FakeOllama) -> None:
    emb = await started(ollama, keep_alive="30s")
    await emb.embed(["x"])
    assert ollama.bodies("/api/embed")[-1]["keep_alive"] == "30s"


async def test_a_wrong_number_of_vectors_is_an_error(ollama: FakeOllama) -> None:
    emb = await started(ollama)
    emb._client._http._transport = httpx2.MockTransport(  # type: ignore[union-attr]
        lambda r: httpx2.Response(200, json={"embeddings": [[0.0] * DIMS]})
    )
    with pytest.raises(AIBackendError, match="wrong number"):
        await emb.embed(["a", "b"])


async def test_a_5xx_is_retried_then_succeeds(ollama: FakeOllama) -> None:
    emb = await started(ollama)
    ollama.calls.clear()
    ollama.fail[:] = [503, 500]
    assert len(await emb.embed(["x"])) == 1
    assert ollama.paths() == ["/api/embed"] * 3


async def test_a_5xx_gives_up_after_max_retries(ollama: FakeOllama) -> None:
    emb = await started(ollama, max_retries=1)
    ollama.calls.clear()
    ollama.fail[:] = [500, 500, 500]
    with pytest.raises(AIBackendError, match="500"):
        await emb.embed(["x"])
    assert len(ollama.calls) == 2


async def test_a_connection_error_is_retried_then_reported(ollama: FakeOllama) -> None:
    emb = await started(ollama)
    ollama.calls.clear()
    ollama.fail[:] = [httpx2.ConnectError("refused")]
    assert len(await emb.embed(["x"])) == 1
    ollama.calls.clear()
    ollama.fail[:] = [httpx2.ConnectError("refused")] * 3
    with pytest.raises(AIBackendError, match="is it running"):
        await emb.embed(["x"])
    assert len(ollama.calls) == 3


async def test_a_4xx_is_not_retried(ollama: FakeOllama) -> None:
    emb = await started(ollama)
    ollama.calls.clear()
    ollama.fail[:] = [400, 200]
    with pytest.raises(AIBackendError, match="400"):
        await emb.embed(["x"])
    assert len(ollama.calls) == 1


async def test_a_timeout_is_not_retried_and_leaks_nothing(ollama: FakeOllama) -> None:
    emb = await started(ollama)
    ollama.calls.clear()
    ollama.fail[:] = [httpx2.ReadTimeout("a secret body went here")]
    with pytest.raises(AIBackendError) as exc:
        await emb.embed(["x"])
    assert "secret" not in str(exc.value) and len(ollama.calls) == 1


async def test_health_reflects_reachability(ollama: FakeOllama) -> None:
    emb = await started(ollama, max_retries=0)
    assert (await emb.health()).ok
    ollama.fail[:] = [httpx2.ConnectError("down")]
    assert not (await emb.health()).ok


def test_it_is_registered_as_an_embedder_entry_point() -> None:
    from importlib.metadata import entry_points

    (ep,) = [e for e in entry_points(group="oaken_core.embedders") if e.name == "ollama"]
    assert ep.load() is OllamaEmbedder
