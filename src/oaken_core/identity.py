# SPDX-License-Identifier: Apache-2.0
"""Who this process is: the app name (it names the home folder and its environment variable), the title
the console shows, the command name, and the command that starts its core in the background. A product built on
core calls ``configure`` once, at start, before anything reads a path. The defaults are plain Oaken Core, so
nothing changes for it."""

import sys

DEFAULT_APP = "oaken"

app = DEFAULT_APP  # "oaken-grove": the platform folders, and ``OAKEN_GROVE_HOME``
title = "Oaken Core"  # "Oaken CRM": the console's wordmark, page titles and footer
prog = "oaken"  # the command: usage and error text
daemon: tuple[str, ...] = ()  # what `start` runs in the background; empty: core's own `python -m oaken_core.daemon`


def configure(*, app: str = DEFAULT_APP, title: str = "Oaken Core", prog: str | None = None, daemon: "list[str] | tuple[str, ...]" = ()) -> None:
    """``daemon`` is the argv that runs this app's core in the foreground (``[sys.executable, "-m", "grove"]``). The child that
    `start` spawns is a new process with no memory of this call, so it must be a program that calls ``configure`` itself."""
    g = globals()
    g["app"], g["title"], g["prog"], g["daemon"] = app, title, prog or app, tuple(daemon)


def daemon_command() -> list[str]:
    return list(daemon) or [sys.executable, "-m", "oaken_core.daemon"]


def home_env() -> str:
    """The variable that relocates this app's home. A product has its own, so it never lands in another
    Oaken's folder just because ``OAKEN_HOME`` is set for a different one."""
    return "OAKEN_HOME" if app == DEFAULT_APP else app.upper().replace("-", "_") + "_HOME"


def wordmark() -> tuple[str, str]:
    """The title as the masthead draws it: the first word, then the rest in the accent colour."""
    first, _, rest = title.partition(" ")
    return first, rest
