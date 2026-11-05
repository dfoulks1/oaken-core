# SPDX-License-Identifier: Apache-2.0
"""The core operations, and the function that puts them in a registry."""

from oaken_core.operations.builtin import agents, labels, queue, secrets, settings, system
from oaken_core.operations.registry import AnyOperation, OperationRegistry


def core_operations() -> tuple[AnyOperation, ...]:
    return (*queue.OPERATIONS, *agents.OPERATIONS, *labels.OPERATIONS, *system.OPERATIONS, *settings.OPERATIONS, *secrets.OPERATIONS)


def core_registry() -> OperationRegistry:
    """A registry holding every core operation. Plugin operations are attached by the daemon."""
    return OperationRegistry(core_operations())


__all__ = ["core_operations", "core_registry"]
