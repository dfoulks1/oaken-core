# SPDX-License-Identifier: Apache-2.0
import pytest
from oaken_core.clock import FakeClock
from oaken_core.errors import PluginError
from oaken_core.intake import IntakeItem
from oaken_core.plugins.contracts import PluginContext
from oaken_core.triage.rules import RulesTriager, TryIn, Nothing, When, matches

RULES = {"rules": [
    {"name": "noise", "when": {"field_contains": {"company": "test"}}, "action": "reject", "learn": True},
    {"name": "hiring", "when": {"source": "voltpop*", "field_equals": {"signal": "hiring"}}, "labels": {"priority": "high"}},
    {"name": "off", "enabled": False, "when": {"source": "voltpop"}, "action": "reject"},
    {"name": "secret", "when": {"has_label": "confidential=true"}, "action": "unsure", "reason": "keep it local"},
]}


def item(body: object, source: str = "voltpop", **labels: str) -> IntakeItem:
    return IntakeItem("s1", source, body, labels)


async def started(config: object = RULES) -> RulesTriager:
    t = RulesTriager()
    await t.start(PluginContext(name="rules", kind="triager", clock=FakeClock(), log=None, config=dict(config)))  # type: ignore[arg-type, call-overload]
    return t


async def test_the_first_matching_rule_decides_and_an_item_no_rule_matches_passes_on() -> None:
    t = await started()
    v = await t.triage(item({"company": "Test Corp", "signal": "hiring"}))
    assert v is not None and (v.kind, v.reason, v.learn) == ("reject", "rule noise", True)  # noise is listed first
    v = await t.triage(item({"company": "Acme", "signal": "hiring"}))
    assert v is not None and (v.kind, v.labels) == ("accept", {"priority": "high"})
    assert await t.triage(item({"company": "Acme", "signal": "layoff"})) is None  # the disabled rule never fires
    v = await t.triage(item("anything", source="other", confidential="true"))
    assert v is not None and (v.kind, v.reason) == ("unsure", "keep it local")


@pytest.mark.parametrize(
    ("when", "body", "source", "labels", "expected"),
    [
        ({"source": "volt"}, "x", "voltpop", {}, True),  # text: contains
        ({"source": "VOLT*"}, "x", "voltpop", {}, True),  # glob, any case
        ({"source": "pop"}, "x", "other", {}, False),
        ({"body_contains": "HELLO"}, "say hello", "s", {}, True),
        ({"body_contains": "acme"}, {"company": "Acme"}, "s", {}, True),  # a JSON body is searched as text
        ({"field_equals": {"a.b": 2}}, {"a": {"b": 2}}, "s", {}, True),
        ({"field_equals": {"a": 1}}, {"a": True}, "s", {}, False),  # 1 is not True
        ({"field_equals": {"a": "1"}}, {"a": 1}, "s", {}, False),  # "1" is not 1
        ({"field_equals": {"a.0": "x"}}, {"a": ["x"]}, "s", {}, True),  # a list, by index
        ({"field_contains": {"a": "ell"}}, {"a": "Hello"}, "s", {}, True),
        ({"field_present": ["a.b"]}, {"a": {"b": None}}, "s", {}, True),
        ({"field_present": ["a.c"]}, {"a": {"b": 1}}, "s", {}, False),
        ({"field_equals": {"a": 1}}, "text body", "s", {}, False),  # a field of a body with no fields
        ({"has_label": "k"}, "x", "s", {"k": "v"}, True),
        ({"has_label": "k=v"}, "x", "s", {"k": "w"}, False),
        ({"source": "s", "body_contains": "nope"}, "x", "s", {}, False),  # every condition must hold
    ],
)  # fmt: skip
def test_each_condition(when: dict, body: object, source: str, labels: dict, expected: bool) -> None:
    assert matches(item(body, source, **labels), When(**when)) is expected


@pytest.mark.parametrize(
    "bad",
    [
        {"rules": [{"name": "x", "when": {}}]},  # no condition
        {"rules": [{"name": "x", "when": {"nope": 1}}]},  # an unknown condition
        {"rules": [{"name": "x", "when": {"source": "a"}, "action": "explode"}]},
        {"rules": [{"name": "x", "when": {"source": "a"}}, {"name": "x", "when": {"source": "b"}}]},  # a name twice
    ],
)
async def test_a_mistake_in_the_rules_stops_the_plugin_with_a_reason(bad: dict) -> None:
    with pytest.raises(PluginError, match="invalid"):
        await started(bad)


async def test_list_and_test_say_what_the_rules_would_do_without_doing_it() -> None:
    t = await started()
    rows = (await t.list_rules(None, Nothing())).rules  # type: ignore[arg-type]
    assert [(r.place, r.name, r.enabled) for r in rows] == [(1, "noise", True), (2, "hiring", True), (3, "off", False), (4, "secret", True)]
    hit = await t.try_item(None, TryIn(source="voltpop", body='{"signal": "hiring"}'))  # type: ignore[arg-type]
    assert (hit.matched, hit.verdict, hit.labels) == ("hiring", "accept", {"priority": "high"})
    miss = await t.try_item(None, TryIn(body="plain text"))  # type: ignore[arg-type]
    assert miss.matched is None and "next triager" in miss.reason
    assert {o.name for o in t.operations()} == {"list", "test", "check"}
    assert all(o.permission == "read" for o in t.operations())


async def test_the_tester_checks_a_typed_rule_and_the_saved_rules_against_a_pasted_item() -> None:
    from oaken_core.triage.rules import CheckIn

    t = await started()
    body = '{"company": "Acme", "signal": "hiring", "size": 3}'
    out = await t.check(None, CheckIn(source="voltpop*", field_equals=["size=3", "signal=layoff"], payload=body, item_source="voltpop"))  # type: ignore[arg-type]
    assert not out.matches and [c.holds for c in out.checks] == [True, True, False]  # size 3 is a number; signal differs
    assert out.saved is not None and out.saved.matched == "hiring"  # the payload also meets a saved rule
    assert (await t.check(None, CheckIn(payload=body))).saved is not None  # type: ignore[arg-type]  # no rule typed: just the saved ones
    assert "at least one condition" in (await t.check(None, CheckIn())).problem  # type: ignore[arg-type]
    assert t.ui().pages[0].test == "check"
