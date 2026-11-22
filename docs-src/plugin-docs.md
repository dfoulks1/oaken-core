+++
title = "Documenting a plugin"
subtitle = "What every plugin ships, and the templates to copy"
lead = "A plugin carries its own record: a README, the decisions that shaped it and the prompts it was built with. Copy the two templates below, fill them in and keep them with the plugin."
group = "start"
order = 14
description = "The documentation a plugin ships (README, decisions, prompts), with the ADR (MADR) and prompt templates to copy."
+++

## What a plugin ships

```text
oaken_plugins/triager_kev/
  __init__.py          the plugin: docstring, kind, name, tags, config model
  docs/
    README.md          what it is for, its settings, an example, known limits
    decisions/         why it works this way: one decision per file
      0001-short-title.md
    prompts/           the prompts it was built with, in order
      01-short-name.md
```

In `oaken-plugins` the folder sits in the plugin's subpackage. In your own package it sits beside `pyproject.toml`.
The settings and commands of a plugin document themselves (`plugin describe`), so the README does not repeat them.
These pages and [Writing a plugin](plugins.md) are the public documentation. A plugin's `docs/` is its internal record.

Write a **decision** when you choose between options a later reader would otherwise reopen: a rule's finality,
what counts as confidential, which library. Never edit an accepted decision's text: a change is a new decision that
supersedes it. Write a **prompt** for any job an agent did, so the work can be reproduced. Say in its first line that
AI wrote it and whether a person has reviewed it.

## The decision template

Decisions use [MADR](https://adr.github.io/madr/). Number the files `0001-`, `0002-` within the plugin.

{{file:docs-src/decisions/TEMPLATE.md}}

## The prompt template

One prompt, one job. Number the files in the order they were run.

{{file:docs-src/prompts/TEMPLATE.md}}
