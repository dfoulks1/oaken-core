# SPDX-License-Identifier: Apache-2.0
"""``run``: the one call a product's command makes.

    # grove/cli.py, and ``grove = "grove.cli:main"`` in its pyproject
    from oaken_core import run

    def main() -> int:
        return run(app="oaken-grove", title="Oaken CRM", prog="grove", queues_from_config=False)

It is core's whole command line (``start``, ``stop``, ``reload``, ``operations``, plugin verbs) under the
product's own name and home folder. Plugins are found through entry points, so a product's plugins are
whatever its package installs.
"""

import functools
from collections.abc import Sequence

from oaken_core import identity
from oaken_core.daemon.core import Core
from oaken_core.plugins.host import Discover, discover_entry_points


def run(
    argv: Sequence[str] | None = None,
    *,
    app: str = identity.DEFAULT_APP,
    title: str = "Oaken Core",
    prog: str | None = None,
    discover: Discover = discover_entry_points,
    queues_from_config: bool = True,
) -> int:
    """Run the command line as ``prog`` and return its exit code. ``app`` names the home folder (and its
    ``<APP>_HOME`` variable), ``title`` is what the console calls the product."""
    identity.configure(app=app, title=title, prog=prog)
    from oaken_core.cli.commands import main

    make = functools.partial(Core, discover=discover, queues_from_config=queues_from_config)
    return main(list(argv) if argv is not None else None, make=make)
