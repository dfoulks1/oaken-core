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
