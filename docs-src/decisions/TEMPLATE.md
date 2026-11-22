---
status: proposed
date: YYYY-MM-DD
decision-makers: the people who decided
---

# The decision, as a short title

## Context and Problem Statement

The situation and the question that needs an answer, in two or three sentences.

## Decision Drivers

* a force or concern that shapes the choice
* another one

## Considered Options

* option one
* option two
* option three

## Decision Outcome

Chosen option: "option one", because it meets the drivers best (say how).

### Consequences

* Good, because …
* Bad, because …

### Confirmation

How the team will know the decision was carried out (a review, a test, a check).

## Pros and Cons of the Options

### option one

* Good, because …
* Bad, because …

### option two

* Good, because …
* Bad, because …

## More Information

Links, related decisions, and when to revisit this one.

---

This layout follows MADR, Markdown Architectural Decision Records (https://adr.github.io/madr/). `adr-index.py`
reads the `status:` line; `adr-check.py` checks only that a status is present.
