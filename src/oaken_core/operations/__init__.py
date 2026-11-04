# SPDX-License-Identifier: Apache-2.0
"""The operation registry and the confirm gate. Trimmed from oaken-mail: the client, services,
exposure, limits and context are mail-shaped and are rewritten for core, not copied."""

from oaken_core.operations.caller import CLIENTS, CallerContext, Client
from oaken_core.operations.gate import InMemoryProposals, Proposal, ProposalStore
from oaken_core.operations.registry import (
    Change,
    McpMode,
    Operation,
    OperationDefinitionError,
    OperationRegistry,
    Permission,
    Preview,
    operation,
)

__all__ = [
    "CLIENTS", "CallerContext", "Change", "Client", "InMemoryProposals", "McpMode", "Operation",
    "OperationDefinitionError", "OperationRegistry", "Permission", "Preview", "Proposal",
    "ProposalStore", "operation",
]
