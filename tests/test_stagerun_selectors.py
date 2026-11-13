# SPDX-License-Identifier: Apache-2.0
from oaken_core.stagerun import selected


def test_when_needs_every_entry_and_unless_none():
    labels = {"sieve.labels": "long, work", "sentiment.score": "1", "mail.starred": "false"}
    assert selected(labels)
    assert selected(labels, when=["long", "sentiment.score=1"])
    assert not selected(labels, when=["long", "missing"])
    assert not selected(labels, unless=["work"])
    assert selected(labels, unless=["mail.starred", "list"])  # "false" is not set


async def test_a_label_an_enricher_sets_travels_with_the_item() -> None:
    from oaken_core.actors import SYSTEM
    from oaken_core.stagerun import StagePayload, StageRunner
    from oaken_core.stages import Enrichment

    class Labels:
        async def set(self, *a: object) -> None: ...

    runner = StageRunner(None, Labels(), enrichers_for=lambda st: [], actions_for=lambda r: [])  # type: ignore[arg-type]
    p = await runner._record(StagePayload(subject_id="s", source="t", labels={"a": "1"}), "tone", Enrichment(data={}, labels={"confidential": "true"}), SYSTEM)
    assert p.labels == {"a": "1", "confidential": "true"}  # a later stage's when/unless, and the agent claim, see it
