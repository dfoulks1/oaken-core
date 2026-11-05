# SPDX-License-Identifier: Apache-2.0
"""Who this process is: the app name (it names the home folder and its environment variable), the title
the console shows, and the command name. A product built on core calls ``configure`` once, at start,
before anything reads a path. The defaults are plain Oaken Core, so nothing changes for it."""

DEFAULT_APP = "oaken"

app = DEFAULT_APP  # "oaken-grove": the platform folders, and ``OAKEN_GROVE_HOME``
title = "Oaken Core"  # "Oaken CRM": the console's wordmark, page titles and footer
prog = "oaken"  # the command: usage and error text


def configure(*, app: str = DEFAULT_APP, title: str = "Oaken Core", prog: str | None = None) -> None:
    g = globals()
    g["app"], g["title"], g["prog"] = app, title, prog or app


def home_env() -> str:
    """The variable that relocates this app's home. A product has its own, so it never lands in another
    Oaken's folder just because ``OAKEN_HOME`` is set for a different one."""
    return "OAKEN_HOME" if app == DEFAULT_APP else app.upper().replace("-", "_") + "_HOME"


def wordmark() -> tuple[str, str]:
    """The title as the masthead draws it: the first word, then the rest in the accent colour."""
    first, _, rest = title.partition(" ")
    return first, rest
