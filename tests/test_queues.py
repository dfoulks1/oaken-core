# SPDX-License-Identifier: Apache-2.0
import pytest

from oaken_core.errors import ConfigError, InvalidInput
from oaken_core.labels import LabelError
from oaken_core.queues import DEFAULT, Queues


def test_default_exists_and_a_new_queue_is_added() -> None:
    q = Queues()
    assert [s.name for s in q.all()] == [DEFAULT]
    spec = q.new_queue("enrichers", "derive things", workers=4)
    assert q.get("enrichers") == spec and spec.workers == 4


def test_names_must_be_plain_and_unique() -> None:
    q = Queues()
    q.new_queue("ocr")
    for bad in ("OCR", "has space", "", "1x"):
        with pytest.raises(ConfigError):
            q.new_queue(bad)
    with pytest.raises(ConfigError, match="already exists"):
        q.new_queue("ocr")
    with pytest.raises(ConfigError, match="already exists"):
        q.new_queue(DEFAULT)  # the default can be neither redefined nor removed
    with pytest.raises(ConfigError, match="worker"):
        q.new_queue("none", workers=0)


def test_a_missing_queue_is_refused_and_the_label_check_agrees() -> None:
    q = Queues()
    with pytest.raises(InvalidInput):
        q.get("nope")
    q.validate_label(DEFAULT)
    with pytest.raises(LabelError, match="no queue named"):
        q.validate_label("nope")


# -- queues from config.toml: one analyzer kind each --------------------------------------------
import pytest  # noqa: E402

from oaken_core.daemon.core import Core  # noqa: E402
from oaken_core.errors import ConfigError  # noqa: E402
from oaken_core.settings import Settings  # noqa: E402

STAGES = {"queues": {
    "triage": {"kind": "triager", "workers": 2},
    "enrich": {"kind": "enricher", "label": "Enrichment", "description": "adds facts"},
}}


async def opened(tmp_path, **kw):  # type: ignore[no-untyped-def]
    from oaken_core.store import SqliteStore

    from .test_host import EP

    tables = {"store": {"sqlite": {"path": str(tmp_path / "core.db")}}}
    core = Core(Settings.model_validate({"console": {"port": 0}, "plugins": {"tables": tables}, **kw.pop("settings", {})}),
                discover=lambda g: {"oaken_core.stores": [EP("sqlite", SqliteStore)]}.get(g, []), **kw)
    return core


async def test_queues_from_config_come_in_file_order_each_with_its_kind(tmp_path) -> None:  # type: ignore[no-untyped-def]
    core = await opened(tmp_path, settings=STAGES)
    await core.open()
    try:
        specs = core.client.svc.queues.all()
        assert [q.name for q in specs] == ["default", "triage", "enrich"]
        assert [(q.kind, q.workers, q.title) for q in specs[1:]] == [("triager", 2, "Triage"), ("enricher", 1, "Enrichment")]
    finally:
        await core.stop()


@pytest.mark.parametrize(("table", "why"), [
    ({"x": {"kind": "embedder"}}, "not an analyzer"),
    ({"x": {"kind": "nope"}}, "not a plugin kind"),
    ({"default": {"kind": "triager"}}, "already exists"),
])
async def test_a_queue_for_anything_but_a_known_analyzer_kind_is_refused(tmp_path, table, why) -> None:  # type: ignore[no-untyped-def]
    core = await opened(tmp_path, settings={"queues": table})
    with pytest.raises(ConfigError, match=why):
        await core.open()
    await core.stop()


async def test_a_product_can_turn_config_queues_off(tmp_path) -> None:  # type: ignore[no-untyped-def]
    core = await opened(tmp_path, settings=STAGES, queues_from_config=False)
    with pytest.raises(ConfigError, match="defines its own queues"):
        await core.open()
    await core.stop()
    fine = await opened(tmp_path, queues_from_config=False)  # no tables: nothing to refuse
    await fine.open()
    await fine.stop()


def test_a_plugin_kind_belongs_to_one_queue_so_each_plugin_has_one_stage() -> None:
    from oaken_core.queues import Queues

    q = Queues()
    q.new_queue("triage", kind="triager")
    with pytest.raises(ConfigError, match="already serves 'triager'"):
        q.new_queue("triage-slow", kind="triager")
    q.new_queue("enrich", kind="enricher")  # another kind is fine
    q.new_queue("plain")  # a queue with no kind is not a stage


# -- queue_add ---------------------------------------------------------------------------------
import tomllib  # noqa: E402
import types  # noqa: E402

from oaken_core import paths  # noqa: E402
from oaken_core.errors import Conflict, InvalidInput  # noqa: E402
from oaken_core.operations.caller import CallerContext  # noqa: E402
from oaken_core.plugins.kinds import KindRegistry  # noqa: E402

from .conftest import Ops  # noqa: E402


def with_host(ops: Ops) -> None:
    ops.svc.host = types.SimpleNamespace(kinds=KindRegistry.shipped(), check_config=lambda n, t: [], statuses=lambda: [])  # type: ignore[assignment]


def saved() -> dict:  # type: ignore[type-arg]
    return tomllib.loads((paths.config_dir() / "config.toml").read_text())


async def test_queue_add_asks_first_then_writes_the_table_and_reloads(ops: Ops) -> None:
    with_host(ops)
    reloads: list[int] = []
    ops.svc.reload = lambda: reloads.append(1)
    cli = CallerContext.cli()
    args = {"name": "triage", "kind": "triager", "label": "Triage", "workers": 2}
    asked = await ops.client.call("queue_add", args, cli)
    assert asked.proposal is not None and "triager" in asked.proposal.summary and not (paths.config_dir() / "config.toml").exists()
    done = (await ops.client.call("queue_add", args, cli, confirm=True)).output
    assert done.reloading and reloads == [1]  # type: ignore[union-attr]
    assert saved()["queues"]["triage"] == {"kind": "triager", "label": "Triage", "workers": 2}


async def test_queue_add_says_to_restart_when_core_was_not_started_by_the_daemon(ops: Ops) -> None:
    with_host(ops)
    out = (await ops.client.call("queue_add", {"name": "enrich", "kind": "enricher"}, CallerContext.cli(), confirm=True)).output
    assert not out.reloading and "restart" in out.note  # type: ignore[union-attr]


async def test_queue_add_refuses_what_the_file_or_the_product_would_not_take(ops: Ops) -> None:
    with_host(ops)
    cli = CallerContext.cli()
    add = lambda **kw: ops.client.call("queue_add", kw, cli, confirm=True)  # noqa: E731
    await add(name="triage", kind="triager")
    for bad, exc in [
        ({"name": "again", "kind": "triager"}, InvalidInput),  # a kind belongs to one queue
        ({"name": "x", "kind": "embedder"}, InvalidInput),  # not an analyzer
        ({"name": "x", "kind": "nope"}, InvalidInput),
        ({"name": "a/b", "kind": "enricher"}, InvalidInput),  # a character a name cannot have
        ({"name": "9lives", "kind": "enricher"}, InvalidInput),  # must start with a letter
        ({"name": "default", "kind": "enricher"}, Conflict),  # exists already
    ]:
        with pytest.raises(exc):
            await add(**bad)
    assert list(saved()["queues"]) == ["triage"]  # nothing else was written
    ops.svc.queues_from_config = False
    with pytest.raises(Conflict, match="own queues"):
        await add(name="enrich", kind="enricher")


async def test_queue_add_is_never_offered_over_mcp(ops: Ops) -> None:
    assert "queue_add" not in {o.name for o in await ops.client.visible(CallerContext.mcp("me"))}


async def test_queue_add_turns_what_was_typed_into_a_name_and_keeps_it_as_the_title(ops: Ops) -> None:
    with_host(ops)
    out = (await ops.client.call("queue_add", {"name": "  My Slow  Stage ", "kind": "enricher"}, CallerContext.cli(), confirm=True)).output
    assert out.name == "my_slow_stage"  # type: ignore[union-attr]  # lower case, a run of spaces as one underscore
    assert saved()["queues"]["my_slow_stage"] == {"kind": "enricher", "label": "My Slow  Stage", "workers": 1}
    asked = await ops.client.call("queue_add", {"name": "Second One", "kind": "action"}, CallerContext.cli())
    assert asked.proposal is not None and "second_one" in asked.proposal.summary  # the confirm step shows the name it will have
