# SPDX-License-Identifier: Apache-2.0
import pytest

from oaken_core.actors import SYSTEM, USER, Actor
from oaken_core.changelog import ChangeLog
from oaken_core.clock import FakeClock
from oaken_core.db import Database
from oaken_core.labels import BaseLabel, LabelError, LabelMaker, LabelStore, ReservedLabel

AGENT = Actor("agent", "librarian")
S = "subject-1"


@pytest.fixture
def maker() -> LabelMaker:
    m = LabelMaker.core()
    m.register(BaseLabel.new("heat", "How upset", ("calm", "warm", "hot")))
    return m


@pytest.fixture
def labels(core_db: Database, maker: LabelMaker, clock: FakeClock) -> LabelStore:
    return LabelStore(core_db, maker, ChangeLog(clock), clock)


async def test_machine_value_shows_when_no_human_value(labels: LabelStore) -> None:
    await labels.set(S, "priority", "low", SYSTEM)
    assert await labels.get(S, "priority") == "low"


async def test_a_human_value_wins_and_survives_machine_reruns(labels: LabelStore) -> None:
    await labels.set(S, "priority", "low", SYSTEM)
    await labels.set(S, "priority", "urgent", USER)
    for value in ("normal", "high", "low", "normal"):  # an enricher re-running
        await labels.set(S, "priority", value, SYSTEM)
        assert await labels.get(S, "priority") == "urgent"
    assert await labels.layers(S, "priority") == {"human": "urgent", "machine": "normal"}


async def test_clearing_the_human_layer_reverts_to_the_machine_value(labels: LabelStore) -> None:
    await labels.set(S, "priority", "low", SYSTEM)
    await labels.set(S, "priority", "urgent", USER)
    assert await labels.clear_human(S, "priority", USER) is True
    assert await labels.get(S, "priority") == "low"
    assert await labels.clear_human(S, "priority", USER) is False  # nothing left to clear


async def test_clearing_the_machine_layer_keeps_the_human_value(labels: LabelStore) -> None:
    await labels.set(S, "priority", "urgent", USER)
    await labels.set(S, "priority", "low", SYSTEM)
    await labels.set(S, "heat", "hot", AGENT)
    assert await labels.clear_machine(S, SYSTEM) == 2
    assert await labels.all(S) == {"priority": "urgent"}


async def test_all_reports_effective_values(labels: LabelStore) -> None:
    await labels.set(S, "priority", "low", SYSTEM)
    await labels.set(S, "priority", "high", USER)
    await labels.set(S, "heat", "warm", AGENT)
    assert await labels.all(S) == {"priority": "high", "heat": "warm"}


async def test_every_changing_write_has_one_log_row(
    labels: LabelStore, core_db: Database, clock: FakeClock
) -> None:
    writes = [
        ("priority", "low", SYSTEM), ("priority", "high", USER), ("heat", "hot", AGENT),
        ("priority", "normal", SYSTEM), ("heat", "calm", AGENT),
    ]  # fmt: skip
    for key, value, actor in writes:
        await labels.set(S, key, value, actor)
    await labels.clear_human(S, "priority", USER)
    rows = await labels.log.rows(core_db, S)
    assert len(rows) == len(writes) + 1
    assert [r.op for r in rows].count("clear") == 1
    assert rows[1].layer == "human" and rows[1].actor_kind == "user"
    assert rows[2].actor == "librarian"


async def test_a_write_that_changes_nothing_logs_nothing(
    labels: LabelStore, core_db: Database
) -> None:
    assert await labels.set(S, "priority", "low", SYSTEM) is True
    assert await labels.set(S, "priority", "low", SYSTEM) is False
    assert len(await labels.log.rows(core_db, S)) == 1


async def test_unknown_key_and_bad_value_raise(labels: LabelStore) -> None:
    with pytest.raises(LabelError, match="unknown"):
        await labels.set(S, "nope", "x", SYSTEM)
    with pytest.raises(LabelError, match="not one of"):
        await labels.set(S, "priority", "soon", SYSTEM)
    assert await labels.get(S, "priority") is None  # the failed write stored nothing


def test_a_plugin_cannot_define_a_reserved_key(maker: LabelMaker) -> None:
    with pytest.raises(LabelError, match="reserved"):
        maker.register(BaseLabel.new("queue"))
    with pytest.raises(LabelError, match="only core"):
        maker.register(ReservedLabel.new("mine"))


def test_redefining_a_key_differently_raises_but_a_reload_is_fine(maker: LabelMaker) -> None:
    maker.register(BaseLabel.new("heat", "How upset", ("calm", "warm", "hot")))  # identical
    with pytest.raises(LabelError, match="differently"):
        maker.register(BaseLabel.new("heat", "other"))


def test_a_malformed_key_is_refused() -> None:
    for bad in ("Heat", "1a", "has space", ""):
        with pytest.raises(LabelError):
            BaseLabel.new(bad)


async def test_the_queue_validator_runs_on_every_write(labels: LabelStore, maker: LabelMaker) -> None:
    def real_queue(value: str) -> None:
        if value not in {"default", "enrichers"}:
            raise LabelError(f"no queue named {value!r}")

    maker.set_validator("queue", real_queue)
    await labels.set(S, "queue", "enrichers", SYSTEM)
    with pytest.raises(LabelError, match="no queue named"):
        await labels.set(S, "queue", "typo", USER)
    assert await labels.get(S, "queue") == "enrichers"


async def test_keys_lists_every_defined_key_by_name(maker: LabelMaker) -> None:
    keys = [b.key for b in maker.keys()]
    assert keys == sorted(keys) and {"queue", "priority", "confidential", "heat"} <= set(keys)


async def test_find_returns_subjects_with_the_effective_value_and_filters_on_it(labels: LabelStore) -> None:
    await labels.set("a", "heat", "calm", SYSTEM)
    await labels.set("a", "heat", "hot", USER)  # the person's value wins
    await labels.set("b", "heat", "hot", SYSTEM)
    await labels.set("c", "heat", "calm", SYSTEM)
    assert await labels.find("heat") == [("a", "hot"), ("b", "hot"), ("c", "calm")]
    assert await labels.find("heat", "hot") == [("a", "hot"), ("b", "hot")]
    assert await labels.find("heat", "calm") == [("c", "calm")]  # a's machine "calm" is overridden
    assert await labels.find("heat", limit=1) == [("a", "hot")]
    with pytest.raises(LabelError):
        await labels.find("nope")
