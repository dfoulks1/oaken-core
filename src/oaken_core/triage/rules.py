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

A rule may name ``route = ["name"]``: the item is then owed to the action plugin that answers that route.

A rule can also look inside what a formatter made of the item: ``parts.<name>.<path>``. For mail (the ``rfc822`` formatter) that is
``parts.headers.subject.0`` (a header's values are a list, the first is ``.0``), ``parts.text``, ``parts.html`` and ``parts.clean``:

    [[plugins.triager.rules.rules]]
    name = "invoices"
    when = { field_contains = { "parts.headers.subject" = "invoice" } }
    action = "accept"
    labels = { queue = "finance" }

Try a rule without a daemon trial: ``oaken rules test --source voltpop --body '{"signal": "hiring"}'``.

shortcut: rules are edited in ``config.toml`` (or its page in the console), not by commands, because a plugin
has no way to write the config; add ``add``/``remove`` commands when core offers one.
"""

import json
from fnmatch import fnmatchcase
from typing import Any, ClassVar, Literal

from oaken_core.errors import InvalidInput
from oaken_core.intake import IntakeItem, Verdict, item_text
from oaken_core.parts import Part
from oaken_core.plugins.base import BaseTriager, command
from oaken_core.plugins.contracts import PluginOpContext, UiField, UiPage, UiSpec
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Scalar = str | int | float | bool


class When(BaseModel):
    """Every condition given must hold. At least one is needed."""

    model_config = ConfigDict(extra="forbid")

    source: str | None = Field(default=None, description="The source contains this text, or matches a glob such as voltpop*.")
    body_contains: str | None = Field(default=None, description="The body's text contains this (a JSON body is searched as text).")
    field_equals: dict[str, Scalar] | None = Field(default=None, description="JSON body fields that equal these values, by dotted path (company.size). parts.<name>.<path> reaches a formatter part (parts.headers.subject.0).")
    field_contains: dict[str, str] | None = Field(default=None, description="JSON body fields whose text contains these. parts.<name>.<path> reaches a formatter part (parts.text, parts.headers.from.0).")
    field_present: list[str] | None = Field(default=None, description="JSON body fields that exist, by dotted path. parts.<name> reaches a formatter part (parts.headers.list-unsubscribe).")
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
    route: list[str] = Field(default_factory=list, description="Action routes this item is owed: the names an action plugin answers. Empty: none.")
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


def _dig_item(item: IntakeItem, path: str) -> tuple[bool, Any]:
    """``parts.<name>.<path>`` reaches inside what the formatter made (``parts.headers.subject.0``, ``parts.text``); any other path is the body's."""
    if path.startswith("parts."):
        return _dig({name: part.content for name, part in item.parts.items()}, path.removeprefix("parts."))
    return _dig(item.body, path)


def explain(item: IntakeItem, when: When) -> list[tuple[str, bool]]:
    """Each condition of ``when``, as a short line, with whether it holds for ``item``."""
    out: list[tuple[str, bool]] = []
    if when.source is not None:
        out.append((f"source ~ {when.source}", _glob_or_text(when.source, item.source)))
    if when.body_contains is not None:
        out.append((f"body contains {when.body_contains!r}", when.body_contains.lower() in item_text(item).lower()))
    for path, want in (when.field_equals or {}).items():
        found, got = _dig_item(item, path)
        out.append((f"{path} == {want!r}" + ("" if found else " (no such field)"), found and got == want and type(got) is type(want)))  # 1 is not True, "1" is not 1
    for path, text in (when.field_contains or {}).items():
        found, got = _dig_item(item, path)
        out.append((f"{path} contains {text!r}" + ("" if found else " (no such field)"), found and text.lower() in str(got).lower()))
    for path in when.field_present or []:
        out.append((f"{path} is present", _dig_item(item, path)[0]))
    if when.has_label is not None:
        key, _, value = when.has_label.partition("=")
        out.append((f"has label {when.has_label}", key in item.labels and (not value or item.labels[key] == value)))
    return out


def matches(item: IntakeItem, when: When) -> bool:
    return all(ok for _, ok in explain(item, when))


def first_match(item: IntakeItem, rules: list[Rule]) -> Rule | None:
    return next((r for r in rules if r.enabled and matches(item, r.when)), None)


def verdict_of(rule: Rule) -> Verdict:
    return Verdict(rule.action, rule.reason or f"rule {rule.name}", dict(rule.labels), learn=rule.learn and rule.action == "reject", route=tuple(rule.route))


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
    route: list[str] = []


class RulesOut(BaseModel):
    rules: list[RuleRow]


class TryIn(BaseModel):
    source: str = Field(default="test", description="The source the item would come from.")
    body: str = Field(description="The body: JSON if it parses, else text.")
    labels: dict[str, str] = Field(default_factory=dict, description="Labels the item would carry.")
    parts: str = Field(default="", description="What a formatter made of it, as JSON {part name: content}; rules reach it as parts.<name>.")


class TryOut(BaseModel):
    matched: str | None  # the rule's name, or None when no rule matches
    verdict: str | None
    labels: dict[str, str]
    reason: str
    route: list[str] = []


class CheckIn(BaseModel):
    """The tester's form: the conditions of one rule, and an item to try them on."""

    source: str | None = None
    body_contains: str | None = None
    field_equals: list[str] = Field(default_factory=list)
    field_contains: list[str] = Field(default_factory=list)
    field_present: list[str] = Field(default_factory=list)
    has_label: str | None = None
    item_source: str = "test"
    item_labels: list[str] = Field(default_factory=list)
    payload: str = ""
    parts: str = ""


class Check(BaseModel):
    condition: str
    holds: bool


class CheckOut(BaseModel):
    matches: bool
    checks: list[Check]
    problem: str = ""
    toml: str = ""  # the rule, ready to paste under [[plugins.triager.rules.rules]]
    saved: TryOut | None = None  # what the rules in config.toml would do with the payload


def _parts(text: str) -> dict[str, Part]:
    """The tester's pasted JSON {name: content} as the parts a formatter would have made. Empty means none."""
    if not text.strip():
        return {}
    try:
        made = json.loads(text)
    except ValueError as exc:
        raise InvalidInput(f"parts is not valid JSON: {exc}") from None
    if not isinstance(made, dict):
        raise InvalidInput("parts must be a JSON object: {part name: content}")
    return {str(k): Part(name=str(k), content=v) for k, v in made.items()}


def _pairs(lines: list[str]) -> dict[str, str]:
    return {k.strip(): v.strip() for k, _, v in (ln.partition("=") for ln in lines) if k.strip()}


def _typed(text: str) -> Any:
    """``3`` is a number, ``true`` a bool, ``"3"`` the text 3; anything that is not JSON is text."""
    try:
        v = json.loads(text)
    except ValueError:
        return text
    return v if isinstance(v, str | int | float | bool) else text


TESTER = UiPage(
    "tester", "Try a rule", load="list", test="check",
    fields=(
        UiField("source", "source", help="The source contains this text, or matches a glob such as voltpop*."),
        UiField("body_contains", "body_contains", help="The body's text contains this."),
        UiField("field_equals", "field_equals", "list", help="One path=value per line (company.size=3). 3 is a number, true a bool, \"3\" text."),
        UiField("field_contains", "field_contains", "list", help="One path=text per line."),
        UiField("field_present", "field_present", "list", help="One path per line."),
        UiField("has_label", "has_label", help="A key, or key=value."),
        UiField("item_source", "item source", help="The source the pasted item came from."),
        UiField("item_labels", "item labels", "list", help="One key=value per line."),
        UiField("payload", "payload", "textarea", help="The item's body: paste JSON, or plain text."),
        UiField("parts", "parts", "textarea", help="What a formatter made of it, as JSON {part name: content}, e.g. {\"headers\": {\"subject\": [\"Hi\"]}, \"text\": \"...\"}. Rules reach it as parts.headers.subject.0, parts.text."),
    ),
)  # fmt: skip


class RulesTriager(BaseTriager):
    """Runs before the models (rank 5): an item you have a rule for is never sent to one."""
    icon = "bi-list-check"  # header and field rules

    api_version = 1  # the plugin API this was written against (core's PLUGIN_API_VERSION)
    requires_core = ">=0.0.0"  # the oaken-core versions it works with
    version: ClassVar[str] = "1"
    rank: ClassVar[int] = 5
    config_model = RulesConfig
    config: RulesConfig

    async def triage(self, item: IntakeItem) -> Verdict | None:
        rule = first_match(item, self.config.rules)
        return verdict_of(rule) if rule else None

    def ui(self) -> UiSpec:
        return UiSpec(pages=(TESTER,))

    @command("check", "Try one rule's conditions on a pasted item (which hold, and the rule as TOML), and what the saved rules would decide for it. Changes nothing.", input=CheckIn, output=CheckOut)
    async def check(self, ctx: PluginOpContext, inp: CheckIn) -> CheckOut:
        when = {k: v for k, v in {"source": inp.source, "body_contains": inp.body_contains, "has_label": inp.has_label,
                                  "field_equals": {k: _typed(v) for k, v in _pairs(inp.field_equals).items()},
                                  "field_contains": _pairs(inp.field_contains), "field_present": inp.field_present}.items() if v}  # fmt: skip
        try:
            body: Any = json.loads(inp.payload)
        except ValueError:
            body = inp.payload
        item = IntakeItem("test", inp.item_source, body, _pairs(inp.item_labels), parts=_parts(inp.parts))
        saved = await self.try_item(ctx, TryIn(source=inp.item_source, body=inp.payload, labels=_pairs(inp.item_labels), parts=inp.parts)) if inp.payload.strip() or inp.parts.strip() else None
        if not when and saved is not None:  # no rule typed: just the saved ones
            return CheckOut(matches=False, checks=[], saved=saved)
        try:
            cond = When(**when)
        except ValidationError as exc:
            return CheckOut(matches=False, checks=[], problem="; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors()), saved=saved)
        checks = [Check(condition=c, holds=ok) for c, ok in explain(item, cond)]
        return CheckOut(matches=all(c.holds for c in checks), checks=checks, toml="when = " + json.dumps(when), saved=saved)  # shortcut: JSON is valid TOML inline-table text only for plain values, which these are; upgrade if a rule needs dates

    @command("list", "The rules, in the order they run.", input=Nothing, output=RulesOut)
    async def list_rules(self, ctx: PluginOpContext, inp: Nothing) -> RulesOut:
        return RulesOut(rules=[RuleRow(place=i, name=r.name, enabled=r.enabled, action=r.action,
                                       when=r.when.model_dump(exclude_none=True), labels=r.labels, route=r.route)
                               for i, r in enumerate(self.config.rules, 1)])  # fmt: skip

    @command("test", "Say which rule an item would meet and what it would decide. Changes nothing.", input=TryIn, output=TryOut)
    async def try_item(self, ctx: PluginOpContext, inp: TryIn) -> TryOut:
        try:
            body: Any = json.loads(inp.body)
        except ValueError:
            body = inp.body
        item = IntakeItem("test", inp.source, body, dict(inp.labels), parts=_parts(inp.parts))
        rule = first_match(item, self.config.rules)
        if rule is None:
            return TryOut(matched=None, verdict=None, labels={}, reason="no rule matches: the next triager decides")
        v = verdict_of(rule)
        return TryOut(matched=rule.name, verdict=v.kind, labels=v.labels, reason=v.reason, route=list(v.route))
