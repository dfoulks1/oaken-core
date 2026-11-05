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
