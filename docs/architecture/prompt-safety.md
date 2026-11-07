+++
title = "Prompt safety"
subtitle = "How outside text reaches a language model without becoming an instruction"
lead = "Outside text is data. It enters a prompt only inside a fenced block with a fresh random nonce. The instructions are written in code. The answer is JSON that is checked, never run."
badges = [["text-bg-warning", "Alpha"]]
group = "concepts"
order = 38
description = "UntrustedText and its delimiters, the triage llm plugin as the worked example, structured answers, and the confidential rule: local always, lan on an allowlist, hosted never."
+++

## The rule

An item can say anything, including "ignore your instructions". So core keeps item text away from the
instructions:

- The **system prompt** is the plugin's own text. `LLMRequest.system` is for that and nothing else.
- Outside text goes only in the **user turn**, as `UntrustedText` (`repo:src/oaken_core/untrusted.py`).
  It has no `str()` (calling it raises `TypeError`), so it cannot slip into an f-string or a log line.
  The one way to show it to a model is `render()`, which fences it:

  ```text
  <<<DATA label=item nonce=3f9c2a71d04be856>>>
  ...the item text...
  <<<END nonce=3f9c2a71d04be856>>>
  ```

- The nonce is new on every call, so the text cannot know it ahead of time. Text that looks like a
  delimiter is neutralized first. Runs of two or more `<` or `>` (and look-alikes such as full-width
  or angle-quote characters) become `‹` and `›`. Invisible format characters (zero-width,
  bidirectional controls) are removed. A single `<`, as in `<a@b.example>`, stays.
- The plugin's `guard` text tells the model that fenced text is data, never to obey it, and to answer
  only with the JSON the schema asks for. That is a request, not a guarantee.

## The worked example: the llm triager

`repo:src/oaken_core/triage/llm.py` is the pattern. It asks a model whether an item meets the owner's
`criteria`.

- `PROMPT` and `GUARD` are class constants. The owner may override them in the plugin's table
  (`prompt`, `guard`); the plugin reads them with `effective_prompt()` and `effective_guard()`.
- The criteria are the owner's own words. They go in the system text. The item and its source go
  in as two `UntrustedText` parts, labelled `item` and `source`.
- The answer is a pydantic model (`verdict`: accept, reject or unsure; `reason`; `confidence`). The
  JSON schema goes in the request. The reason is cut to twenty words.
- The cache key includes a hash of the criteria, so changing them never returns an old answer.
- `oaken llm try` asks about some text without submitting anything.

A plugin asks through `BaseLLMAnalyzer.ask()`, which hands the request to the [router](router.md).

## Structured answers

The router validates every reply against the plugin's answer model. A reply that is not JSON, or does
not fit, counts as a bad answer. It gets one escalation to a larger model, if the route names one.
Then the call raises `AIBackendError` and stores nothing. Error text names the fields that were wrong
(never the input or the reply). Tokens spent on a bad answer are still counted in the ledger.

Output is capped (`[router] default_output_tokens`, 256 unless the request sets its own). Input is
cut to the backend's context window (and to `default_input_tokens`, 2000). Later parts are cut first.

## Confidential items

An item labelled `confidential = "true"` (a reserved label) must stay off remote machines. The router
reads each backend's `locality` from its table, `[plugins.llm_backend.<name>]`:

| Locality | A confidential call may use it |
| --- | --- |
| `local` | Always. |
| `lan` | Only if its name is in `[router] lan_allowlist`. |
| `hosted` | Never. |

A backend that does not say counts as `hosted`. So does a locality core does not know (it logs an
error). A disallowed backend is skipped, including in `fallback` and `escalate`. If none is left, the
router raises `ConfidentialRefused`. It never falls back to a remote box. The same rule covers the
embedder.

```toml
[router]
lan_allowlist = ["desk"]

[plugins.llm_backend.desk]
plugin = "ollama"
locality = "lan"
```

## What this does not prove

Delimiters, nonces and a schema make an attack harder and make a success visible. They do not make a
model obey. A model can still be talked into a wrong answer, which is why answers are validated,
never executed, and never trigger an action by themselves. The unit tests check what is sent and what
is refused (`tests/test_untrusted.py`, `tests/test_triage_llm.py`). They cannot show how a given model
behaves when it reads hostile text. That needs a live run against a real model. Core does not yet
lint plugin source for prompts built from raw strings (oaken-mail does); a plugin author keeps outside
text out of `system` by hand.

The answer cache holds text derived from items, so treat it as sensitive as the items. `cache_clear`
removes it, all of it or one analyzer's.
