+++
title = "Oaken Core"
subtitle = "An async pipeline and a plugin loader"
lead = "Oaken Core takes items in, formats them, triages them, stores them and runs the work around them. It has no nouns of its own: a message, a person or a CVE is a plugin's idea. Products are thin coats of plugins over it."
badges = [["text-bg-warning", "Pre-alpha"], ["text-bg-secondary", "Local-first"], ["text-bg-primary", "Open source"]]
description = "An async pipeline and a plugin loader with no nouns: providers, formatters, triagers and ingestors are plugins."
+++

## What it is for

- **One pipeline, many products.** Providers bring items in, formatters split them into parts, triagers rate them, an ingestor stores them. Each is a plugin behind a small, declared contract.
- **Nothing happens by accident.** Every step is a durable job, and a change made through any door (CLI, API, MCP, console) is a proposal until a person confirms it.
- **Outside text is data.** Text from outside is never an instruction to the system or to a model.
- **Plugins describe themselves.** A plugin declares its settings, its row, and whether it is a template; the console draws the same row everywhere.

## The documents

{{doc-cards}}
