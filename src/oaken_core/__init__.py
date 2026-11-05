# SPDX-License-Identifier: Apache-2.0
"""Oaken Core: an async pipeline and a plugin loader. Products are plugins on it."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("oaken-core")
except PackageNotFoundError:  # not installed (a bare checkout)
    __version__ = "0.0.0"


def __getattr__(name: str) -> object:  # ``from oaken_core import run`` without importing the daemon at package import
    if name == "run":
        from oaken_core.app import run

        return run
    raise AttributeError(name)
