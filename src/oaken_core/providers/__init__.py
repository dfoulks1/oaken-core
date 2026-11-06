# SPDX-License-Identifier: Apache-2.0
"""The doors core ships so items can come in with nothing else installed: ``socket`` (the machine's: send to a socket, it is
submitted) and ``jsonl`` (by hand: pipe JSON Lines in from the CLI). Intake then formats and verifies what came in. Plugins
like any other: configure, disable or replace."""
