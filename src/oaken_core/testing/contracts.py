# SPDX-License-Identifier: Apache-2.0
"""Contract suites any plugin author can run against their plugin, one per kind.

Shape lifted from oaken-mail ``testing/contracts.py`` (cf3b187). Two ways to use one. As a pytest
mixin, which gives one test per rule:

    class TestMyEmbedder(EmbedderContract):
        @pytest.fixture
        def embedder(self):
            return MyEmbedder()

Or as one call, ``await verify_embedder(MyEmbedder)``, which runs every rule and raises a single
``AssertionError`` listing everything that failed. Only the embedder suite exists so far; the others
join it as each kind gets one (step 7).
"""

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from oaken_core import __version__
from oaken_core.clock import FakeClock
from oaken_core.logging import get_logger
from oaken_core.plugins.contracts import PluginContext
from oaken_core.plugins.kinds import KindRegistry
from oaken_core.plugins.validate import check_plugin, check_started


def make_context(kind: str, name: str = "under-test", config: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(name=name, kind=kind, clock=FakeClock(), log=get_logger("test"), config=config or {})


@contextlib.asynccontextmanager
async def running(plugin: Any, kind: str, config: dict[str, Any] | None = None) -> AsyncIterator[Any]:
    """Start ``plugin`` the way the host does, and stop it after."""
    start = getattr(plugin, "start", None)
    if start:
        await start(make_context(kind, config=config))
    try:
        yield plugin
    finally:
        stop = getattr(plugin, "stop", None)
        if stop:
            await stop()


def _valid(kind: str, plugin: Any) -> None:
    spec = KindRegistry.shipped().get(kind)
    _info, problems = check_plugin(spec, "under-test", plugin, __version__)
    problems += check_started(spec, plugin)
    assert not problems, "; ".join(problems)


# -- Embedder ----------------------------------------------------------------------------------
async def check_embedder_valid(embedder: Any) -> None:
    _valid("embedder", embedder)


async def check_embedder_shape(embedder: Any) -> None:
    texts = ["hello world", "quarterly budget review", "x"]
    vectors = await embedder.embed(texts)
    assert len(vectors) == len(texts), "one vector per text"
    assert all(len(v) == embedder.dimensions for v in vectors), "every vector is `dimensions` long"
    assert all(isinstance(x, float) for v in vectors for x in v), "vectors hold floats"


async def check_embedder_empty_input(embedder: Any) -> None:
    assert await embedder.embed([]) == []


async def check_embedder_deterministic(embedder: Any) -> None:
    a = await embedder.embed(["the same text"])
    b = await embedder.embed(["the same text"])
    assert all(abs(x - y) < 1e-4 for x, y in zip(a[0], b[0], strict=True)), "not deterministic"


EMBEDDER_CHECKS = (
    check_embedder_valid,
    check_embedder_shape,
    check_embedder_empty_input,
    check_embedder_deterministic,
)


async def _verify(
    checks: tuple[Callable[..., Awaitable[None]], ...], make: Callable[[], Any], kind: str,
    config: dict[str, Any] | None,
) -> None:
    """Run every check, each against a fresh plugin, and report all failures together."""
    failures: list[str] = []
    for check in checks:
        try:
            async with running(make(), kind, config) as plugin:
                await check(plugin)
        except Exception as exc:  # AssertionError or anything the plugin raised
            failures.append(f"{check.__name__}: {type(exc).__name__}: {exc}")
    if failures:
        raise AssertionError("contract failures:\n  " + "\n  ".join(failures))


async def verify_embedder(make: Callable[[], Any], config: dict[str, Any] | None = None) -> None:
    await _verify(EMBEDDER_CHECKS, make, "embedder", config)


def _abstract(name: str) -> Any:
    @pytest.fixture
    def fixture(self: Any) -> Any:
        raise NotImplementedError(f"override the {name!r} fixture in your test class")

    fixture.__name__ = name
    return fixture


class EmbedderContract:
    """Override ``embedder`` (the plugin) and, if it needs settings, ``embedder_config``."""

    embedder = _abstract("embedder")

    @pytest.fixture
    def embedder_config(self) -> dict[str, Any] | None:
        return None

    async def test_is_valid(self, embedder: Any, embedder_config: Any) -> None:
        async with running(embedder, "embedder", embedder_config):
            await check_embedder_valid(embedder)

    async def test_shape(self, embedder: Any, embedder_config: Any) -> None:
        async with running(embedder, "embedder", embedder_config):
            await check_embedder_shape(embedder)

    async def test_empty_input(self, embedder: Any, embedder_config: Any) -> None:
        async with running(embedder, "embedder", embedder_config):
            await check_embedder_empty_input(embedder)

    async def test_deterministic(self, embedder: Any, embedder_config: Any) -> None:
        async with running(embedder, "embedder", embedder_config):
            await check_embedder_deterministic(embedder)
