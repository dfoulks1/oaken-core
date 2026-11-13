# SPDX-License-Identifier: Apache-2.0
from oaken_core.intake import Verdict, item_text
from oaken_core.plugins.base import BaseAction, BaseEnricher
from oaken_core.stages import ActionResult, ActionTarget, Enrichment, EnrichTarget


def test_targets_read_like_intake_items_and_actions_add_a_route() -> None:
    t = ActionTarget("s1", "src", {"a": 1}, {}, route="save", enrichments={"x": Enrichment({"k": 1})})
    assert item_text(t) == '{"a": 1}' and t.route == "save" and t.enrichments["x"].data == {"k": 1}
    assert isinstance(t, EnrichTarget) and Verdict("accept", route=("save",)).route == ("save",)


async def test_a_plugin_fills_the_contract() -> None:
    class E(BaseEnricher):
        async def enrich(self, target: EnrichTarget) -> Enrichment | None:
            return Enrichment({"n": len(item_text(target))})

    class A(BaseAction):
        routes = ("save",)

        async def act(self, target: ActionTarget) -> ActionResult:
            return ActionResult(True, f"saved {target.subject_id}")

    assert (await E().enrich(EnrichTarget("s", "x", "abc", {}))) == Enrichment({"n": 3})
    assert (await A().act(ActionTarget("s", "x", "", {}, route="save"))).ok
