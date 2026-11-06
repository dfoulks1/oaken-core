# SPDX-License-Identifier: Apache-2.0
"""``rules``: your own deterministic triage rules, ahead of any model.

Ported from oaken-mail's ``rules`` triager, with its conditions made generic: an item is a ``source``, a body
(text, or JSON) and labels, so a rule says things about those instead of mail headers. Rules run in the order
listed; the first whose conditions all hold decides, so an item the rules settle costs no model call. An item no
rule matches passes to the next triager.

    [plugins.triager.rules]
    [[plugins.triager.rules.rules]]
    name = "hiring signal"
    when = { source = "voltpop*", field_equals = { signal = "hiring" } }
    action = "accept"
    labels = { priority = "high" }

    [[plugins.triager.rules.rules]]
    name = "internal noise"
    when = { field_contains = { company = "test" } }
    action = "reject"

Try a rule without a daemon trial: ``oaken rules test --source voltpop --body '{"signal": "hiring"}'``.

shortcut: rules are edited in ``config.toml`` (or its page in the console), not by commands, because a plugin
has no way to write the config; add ``add``/``remove`` commands when core offers one.
"""

import json
from fnmatch import fnmatchcase
from typing import Any, ClassVar, Literal

from oaken_core.intake import IntakeItem, Verdict, item_text
from oaken_core.plugins.base import BaseTriager, command
from oaken_core.plugins.contracts import PluginOpContext
from pydantic import BaseModel, ConfigDict, Field, model_validator

Scalar = str | int | float | bool


class When(BaseModel):
    """Every condition given must hold. At least one is needed."""

    model_config = ConfigDict(extra="forbid")

    source: str | None = Field(default=None, description="The source contains this text, or matches a glob such as voltpop*.")
    body_contains: str | None = Field(default=None, description="The body's text contains this (a JSON body is searched as text).")
    field_equals: dict[str, Scalar] | None = Field(default=None, description="JSON body fields that equal these values, by dotted path (company.size).")
    field_contains: dict[str, str] | None = Field(default=None, description="JSON body fields whose text contains these.")
    field_present: list[str] | None = Field(default=None, description="JSON body fields that exist, by dotted path.")
    has_label: str | None = Field(default=None, description="The item carries this label: a key, or key=value.")

    @model_validator(mode="after")
    def _says_something(self) -> "When":
        if not self.model_dump(exclude_none=True):
            raise ValueError("a rule needs at least one condition (source, body_contains, field_equals, field_contains, field_present, has_label)")
        return self


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    when: When
    action: Literal["accept", "reject", "unsure"] = Field(default="accept", description="What the rule decides. Reject drops the item; accept and unsure let it through.")
    labels: dict[str, str] = Field(default_factory=dict, description="Labels written to the item (priority=high, queue=triage).")
    reason: str = Field(default="", description="Said in the record. Empty: the rule's name.")
    learn: bool = Field(default=False, description="For a reject: remember its fingerprint so near-copies are dropped without a model.")


class RulesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[Rule] = Field(default_factory=list, description="In the order they run. The first match decides.")

    @model_validator(mode="after")
    def _unique(self) -> "RulesConfig":
        names = [r.name for r in self.rules]
        if dupes := sorted({n for n in names if names.count(n) > 1}):
            raise ValueError(f"rule names must be unique: {', '.join(dupes)}")
        return self


def _glob_or_text(pattern: str, candidate: str) -> bool:
    p, c = pattern.lower(), candidate.lower()
    return fnmatchcase(c, p) if any(ch in p for ch in "*?[") else p in c


def _dig(body: Any, path: str) -> tuple[bool, Any]:
    """(found, value) at a dotted path through a JSON body's objects (and lists, by index)."""
    cur = body
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return False, None
    return True, cur


def matches(item: IntakeItem, when: When) -> bool:
    checks: list[bool] = []
    if when.source is not None:
        checks.append(_glob_or_text(when.source, item.source))
    if when.body_contains is not None:
        checks.append(when.body_contains.lower() in item_text(item).lower())
    for path, want in (when.field_equals or {}).items():
        found, got = _dig(item.body, path)
        checks.append(found and got == want and type(got) is type(want))  # 1 is not True, "1" is not 1
    for path, text in (when.field_contains or {}).items():
        found, got = _dig(item.body, path)
        checks.append(found and text.lower() in str(got).lower())
    for path in when.field_present or []:
        checks.append(_dig(item.body, path)[0])
    if when.has_label is not None:
        key, _, value = when.has_label.partition("=")
        checks.append(key in item.labels and (not value or item.labels[key] == value))
    return all(checks)


def first_match(item: IntakeItem, rules: list[Rule]) -> Rule | None:
    return next((r for r in rules if r.enabled and matches(item, r.when)), None)


def verdict_of(rule: Rule) -> Verdict:
    return Verdict(rule.action, rule.reason or f"rule {rule.name}", dict(rule.labels), learn=rule.learn and rule.action == "reject")


# -- commands ------------------------------------------------------------------------------------
class Nothing(BaseModel):
    pass


class RuleRow(BaseModel):
    place: int
    name: str
    enabled: bool
    action: str
    when: dict[str, Any]
    labels: dict[str, str]


class RulesOut(BaseModel):
    rules: list[RuleRow]


class TryIn(BaseModel):
    source: str = Field(default="test", description="The source the item would come from.")
    body: str = Field(description="The body: JSON if it parses, else text.")
    labels: dict[str, str] = Field(default_factory=dict, description="Labels the item would carry.")


class TryOut(BaseModel):
    matched: str | None  # the rule's name, or None when no rule matches
    verdict: str | None
    labels: dict[str, str]
    reason: str


class RulesTriager(BaseTriager):
    """Runs before the models (rank 5): an item you have a rule for is never sent to one."""

    version: ClassVar[str] = "1"
    rank: ClassVar[int] = 5
    config_model = RulesConfig
    config: RulesConfig

    async def triage(self, item: IntakeItem) -> Verdict | None:
        rule = first_match(item, self.config.rules)
        return verdict_of(rule) if rule else None

    @command("list", "The rules, in the order they run.", input=Nothing, output=RulesOut)
    async def list_rules(self, ctx: PluginOpContext, inp: Nothing) -> RulesOut:
        return RulesOut(rules=[RuleRow(place=i, name=r.name, enabled=r.enabled, action=r.action,
                                       when=r.when.model_dump(exclude_none=True), labels=r.labels)
                               for i, r in enumerate(self.config.rules, 1)])  # fmt: skip

    @command("test", "Say which rule an item would meet and what it would decide. Changes nothing.", input=TryIn, output=TryOut)
    async def try_item(self, ctx: PluginOpContext, inp: TryIn) -> TryOut:
        try:
            body: Any = json.loads(inp.body)
        except ValueError:
            body = inp.body
        item = IntakeItem("test", inp.source, body, dict(inp.labels))
        rule = first_match(item, self.config.rules)
        if rule is None:
            return TryOut(matched=None, verdict=None, labels={}, reason="no rule matches: the next triager decides")
        v = verdict_of(rule)
        return TryOut(matched=rule.name, verdict=v.kind, labels=v.labels, reason=v.reason)
