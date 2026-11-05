# SPDX-License-Identifier: Apache-2.0
"""The sqlite-vec vector store: the contract suite, then what is particular to it.

Lifted in part from oaken-mail ``oaken_vec_sqlite/tests`` (cf3b187), cut down to core's contract."""

from collections.abc import AsyncIterator
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest

from oaken_core.errors import PluginError
from oaken_core.plugins.contracts import VectorItem
from oaken_core.testing.contracts import VectorStoreContract, make_context, verify_vector_store
from oaken_core.vec_sqlite import SqliteVecStore


class TestSqliteVecContract(VectorStoreContract):
    @pytest.fixture
    def store(self, tmp_path: Path) -> SqliteVecStore:
        return SqliteVecStore(tmp_path / "v.db")


async def test_the_one_call_verifier_passes(tmp_path: Path) -> None:
    counter = iter(range(100))
    await verify_vector_store(lambda: SqliteVecStore(tmp_path / f"v{next(counter)}.db"))


@pytest.fixture
async def started(tmp_path: Path) -> AsyncIterator[SqliteVecStore]:
    store = SqliteVecStore(tmp_path / "v.db")
    await store.start(make_context("vector_store"))
    try:
        yield store
    finally:
        await store.stop()


def item(key: str, *vec: float) -> VectorItem:
    return VectorItem(key, vec)


async def test_an_empty_store_answers_with_nothing(started: SqliteVecStore) -> None:
    assert await started.query("c", (1.0, 0.0), 3) == []
    assert (await started.stats()).count == 0
    await started.delete("c", ["x"])  # nothing to delete is not an error


async def test_k_larger_than_the_store_returns_everything_best_first(started: SqliteVecStore) -> None:
    await started.upsert("c", [item("a", 1, 0), item("b", 0.8, 0.2), item("c", 0, 1)])
    hits = await started.query("c", (1.0, 0.0), 50)
    assert [h.key for h in hits] == ["a", "b", "c"]
    assert hits[0].score == pytest.approx(1.0) and hits[2].score == pytest.approx(0.0, abs=1e-6)


async def test_vectors_survive_reopening_the_file(tmp_path: Path) -> None:
    first = SqliteVecStore(tmp_path / "v.db")
    await first.start(make_context("vector_store"))
    await first.upsert("c", [item("a", 1, 0, 0)])
    await first.stop()
    second = SqliteVecStore(tmp_path / "v.db")
    await second.start(make_context("vector_store"))
    try:
        assert [h.key for h in await second.query("c", (1.0, 0.0, 0.0), 1)] == ["a"]
        assert (await second.stats("c")).dimensions == 3
    finally:
        await second.stop()


async def test_an_unstarted_store_says_so() -> None:
    with pytest.raises(PluginError, match="not started"):
        await SqliteVecStore().query("c", (1.0,), 1)


@pytest.mark.parametrize("vec", [(), (0.0, 0.0), (float("nan"), 1.0), (float("inf"), 1.0)])
async def test_vectors_with_no_usable_direction_are_refused(started: SqliteVecStore, vec: tuple[float, ...]) -> None:
    with pytest.raises(PluginError):
        await started.upsert("c", [VectorItem("x", vec)])
    with pytest.raises(PluginError):
        await started.query("c", vec, 1)
    assert (await started.stats()).count == 0


async def test_a_bad_batch_changes_nothing_even_for_a_new_collection(started: SqliteVecStore) -> None:
    with pytest.raises(PluginError):
        await started.upsert("fresh", [item("a", 1, 0), item("b", 1, 0, 0)])
    assert (await started.stats()).collections == {} and await started.query("fresh", (1.0, 0.0), 1) == []
    await started.upsert("fresh", [item("a", 1, 0, 0)])  # and the name was not left holding the wrong size


async def test_cosine_ignores_length(started: SqliteVecStore) -> None:
    await started.upsert("c", [item("short", 1, 0), item("long", 100, 0), item("off", 1, 1)])
    hits = {h.key: h.score for h in await started.query("c", (5.0, 0.0), 3)}
    assert hits["short"] == pytest.approx(1.0) and hits["long"] == pytest.approx(1.0) and hits["off"] < 0.8


async def test_k_must_be_positive(started: SqliteVecStore) -> None:
    with pytest.raises(PluginError, match="at least 1"):
        await started.query("c", (1.0,), 0)


async def test_reads_keep_working_while_writes_happen(started: SqliteVecStore) -> None:
    import asyncio

    await started.upsert("c", [item("seed", 1, 0)])
    writes = [started.upsert("c", [item(f"k{i}", 1, i + 1)]) for i in range(20)]
    reads = [started.query("c", (1.0, 0.0), 3) for _ in range(20)]
    results = await asyncio.gather(*writes, *reads)
    assert all(r and r[0].key for r in results[20:])
    assert (await started.stats("c")).count == 21


async def test_the_health_check_names_the_extension_version(started: SqliteVecStore) -> None:
    health = await started.health()
    assert health.ok and "sqlite-vec" in health.detail


def test_it_is_registered_as_an_entry_point() -> None:
    found = {ep.name: ep.value for ep in entry_points(group="oaken_core.vector_stores")}
    assert found.get("sqlite-vec") == "oaken_core.vec_sqlite:SqliteVecStore"


async def test_starting_without_the_extension_explains_what_to_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "sqlite_vec", None)  # an import of it now fails
    with pytest.raises(PluginError, match="sqlite-vec is missing"):
        await SqliteVecStore(tmp_path / "v.db").start(make_context("vector_store"))


async def test_the_path_can_come_from_the_instances_settings(tmp_path: Path) -> None:
    store = SqliteVecStore()
    where = tmp_path / "elsewhere.db"
    cfg: dict[str, Any] = {"path": str(where)}
    await store.start(make_context("vector_store", config=cfg))
    try:
        await store.upsert("c", [item("a", 1, 0)])
    finally:
        await store.stop()
    assert where.is_file()
