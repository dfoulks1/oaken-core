---
status: proposed
date: 2026-11-22
decision-makers: Drew Foulks
---

# A triager can end triage: `Verdict.final`, and `final = true` on a rule

## Context and Problem Statement

Triage in a stage is "the logicians at the bar" (owner's ruling of 2026-11-06, in `docs-src/PLAN.md`): every triager answers in rank
order, each seeing what the earlier ones said, any `reject` ends triage, an `accept` or `unsure` does not, and the last to answer
decides. That is the default and it stays.

The `rules` triager says something stronger. Its docstring promised that "the first rule that matches decides, so an item the rules
settle costs no model call." Only a reject stopped the later triagers, so a matching `accept` rule still let the LLM triager
(rank 90) run, spent a model call, and could reject an item the owner had explicitly accepted. The pre-1.0 code review found the
contradiction. How does an owner settle an item with a rule, with no model call, without overturning the ruling?

## Decision Drivers

* The 2026-11-06 ruling stands: no change for anyone who does nothing.
* An item a rule settles should cost no model call, as the `rules` docs promise.
* The change must be a backwards-compatible addition to the stable surface (ADR 0010).
* The choice belongs to the owner, rule by rule, not to a plugin's author.

## Considered Options

* Per-rule opt-in: `Verdict.final`, set by `final = true` on a rule (off by default).
* Change the default so an `accept` from `rules` ends triage.
* Fix the docs only.

## Decision Outcome

Chosen option: "per-rule opt-in", because it is the only one that gives an owner a way to settle an item with no model call while
leaving the 2026-11-06 ruling, and every existing config, exactly as it was.

`Verdict` (in `oaken_core.intake`) gains `final: bool = False`. When a triager's answer has `final=True`, `_triage_job` stops asking
the triagers after it, as a reject always did. The `rules` triager reads `final = true` from a rule (`Rule.final`, off by default) and
sets it on that rule's verdict.

### Consequences

* Good, because an owner can write a rule that settles an item with no model call, and the `rules` docs are now true.
* Good, because nothing changes unless a rule sets `final = true`: the ruling is the default.
* Good, because the flag is general: any triager plugin can end triage, not only `rules`.
* Bad, because a `final` verdict skips every triager after it, including any whose only job was to add labels. A rule that sets
  `final` should come after what it must not skip, or the owner should know it ends the stage.
* Bad, because `Verdict` has one more field for plugin authors to know about.

### Confirmation

Tests: `tests/test_triage_rules.py::test_a_rule_says_whether_its_answer_is_the_last_word` (a rule carries `final`, off by default),
`tests/test_intake.py::test_a_final_verdict_ends_triage_so_no_model_is_asked` (a final answer means the model is not asked) and
`tests/test_intake.py::test_a_plain_accept_still_lets_the_model_answer` (the ruling still holds without it).

## Pros and Cons of the Options

### Per-rule opt-in

* Good, because it keeps the ruling and every existing config unchanged.
* Good, because the owner chooses, rule by rule.
* Bad, because it adds a field to `Verdict`.

### Change the default so an `accept` from `rules` ends triage

* Good, because it matches what the `rules` docstring said.
* Bad, because it overturns the owner's ruling of 2026-11-06 and would surprise anyone relying on the model's second opinion.

### Fix the docs only

* Good, because it changes no code.
* Bad, because it leaves no way to settle an item with a rule and no model call.

## More Information

* Related: ADR 0010 (the stable surface and its versioning), ADR 0017 (triagers and enrichers are separate kinds).
* Code: `src/oaken_core/intake.py` (`Verdict.final`, the loop in `_triage_job`), `src/oaken_core/triage/rules.py` (`Rule.final`).
* Found by the pre-1.0 code review, 2026-11-22. Drafted with Claude Sonnet 5.5; awaiting the owner's ratification.
* Revisit when a second triager needs to end triage and the flag proves too blunt (for example, "final for this label only").
