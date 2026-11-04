# SPDX-License-Identifier: Apache-2.0
"""Exit codes and the CLI's own errors.

The exit codes are a stable contract a script can rely on , so they
live in one place and are mapped, not scattered through the commands:

======  ============================================================================
 code    meaning
======  ============================================================================
 0       ok
 1       error — the operation ran and failed, or an unexpected error
 2       usage — bad arguments (argparse already exits 2 for its own parse errors)
 3       core is not running
 4       authentication — a bad API token, or a provider that needs re-authorizing
 5       needs confirmation — a mutation returned a proposal and was not confirmed
======  ============================================================================

The CLI maps core's stable machine ``code`` (``OakenError.code``) onto these. The mapping is the
one place that knows both vocabularies; everything else raises a plain :class:`CliError` with the
exit code already chosen.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1
    USAGE = 2
    CORE_DOWN = 3
    AUTH = 4
    NEEDS_CONFIRMATION = 5


CORE_DOWN_MESSAGE = "core is not running"


class CliError(Exception):
    """Any failure the CLI wants to report with a chosen exit code and a clean message (no
    traceback). ``code`` is the process exit status."""

    def __init__(self, message: str, code: ExitCode = ExitCode.ERROR) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class CoreUnavailable(CliError):
    """Core could not be reached: no runtime file, a dead pid, or a refused connection. Exit 3 with
    the exact :data:`CORE_DOWN_MESSAGE`. Raised by discovery and by a transport that cannot
    connect — never by an operation that merely failed."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(CORE_DOWN_MESSAGE, ExitCode.CORE_DOWN)
        self.detail = detail


# Core error ``code`` → the exit code the CLI reports for it. Anything not listed is exit 1.
# These strings are ``OakenError.code`` values (see ``oaken_core.errors``); kept as literals so the
# CLI does not import the whole error taxonomy just to branch on it.
_ERROR_EXIT: dict[str, ExitCode] = {
    "auth_required": ExitCode.AUTH,
    "invalid_input": ExitCode.USAGE,
    "confirmation_invalid": ExitCode.NEEDS_CONFIRMATION,
    "unavailable": ExitCode.CORE_DOWN,
}


def exit_for_error_code(code: str) -> ExitCode:
    """Map a core error ``code`` to a CLI exit code (default :attr:`ExitCode.ERROR`)."""
    return _ERROR_EXIT.get(code, ExitCode.ERROR)
