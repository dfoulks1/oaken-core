# SPDX-License-Identifier: Apache-2.0
"""Core's console: loopback only, no login, never off."""

from oaken_core.console.app import Console
from oaken_core.console.server import ConsoleServer

__all__ = ["Console", "ConsoleServer"]
