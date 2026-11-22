# SPDX-License-Identifier: Apache-2.0
"""Fixtures for the docs tooling tests."""

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
for p in (TOOLS, HERE):  # tools to import, this dir for treekit
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from treekit import Tree  # noqa: E402


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return Tree(tmp_path / "repo")


@pytest.fixture
def built(tree: Tree, tmp_path: Path) -> Callable[[], Path]:
    """Build the fixture tree and return the output directory."""
    import build_site

    def _build() -> Path:
        out = tmp_path / "out"
        build_site.build(tree.cfg(), out)
        return out

    return _build
