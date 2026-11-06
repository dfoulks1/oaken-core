# SPDX-License-Identifier: Apache-2.0
"""``socket``: core's one provider, the machine's door. It is made to be fed by ``oaken-plugins`` and the like, so it is
extensible but not usable out of the box: it stays off until you set ``unix_path`` or ``port``. What it takes in goes to
Intake, which formats and verifies it. The by-hand door, ``jsonl``, is in ``oaken-plugins``."""
