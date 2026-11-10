# SPDX-License-Identifier: Apache-2.0
from oaken_core.stagerun import selected


def test_when_needs_every_entry_and_unless_none():
    labels = {"sieve.labels": "long, work", "sentiment.score": "1", "mail.starred": "false"}
    assert selected(labels)
    assert selected(labels, when=["long", "sentiment.score=1"])
    assert not selected(labels, when=["long", "missing"])
    assert not selected(labels, unless=["work"])
    assert selected(labels, unless=["mail.starred", "list"])  # "false" is not set
