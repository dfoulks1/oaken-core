# SPDX-License-Identifier: Apache-2.0
"""Errors on the wire: one JSON body, ``{"code": ..., "message": ...}``, and a status per code.

Core's errors carry a stable ``code``; the status is picked here, once, so the OpenAPI document and
the behaviour agree. An error nobody planned for becomes ``internal`` with a fixed message: its text
could quote mail, so it is logged by type and never sent.
"""

from oaken_core.errors import (
    AIBackendError,
    AuthError,
    ConfigError,
    Conflict,
    InvalidInput,
    InvalidResult,
    NotFound,
    OakenError,
    PermissionDenied,
    PluginError,
    ProviderError,
    QuotaExceeded,
    StorageError,
    Unavailable,
)

# Most specific first: ``ConfirmationInvalid`` and ``NotExposed`` are ``PermissionDenied`` (403).
STATUS: tuple[tuple[type[OakenError], int], ...] = (
    (InvalidInput, 422),
    (InvalidResult, 422),
    (PermissionDenied, 403),
    (NotFound, 404),
    (Conflict, 409),
    (QuotaExceeded, 429),
    (Unavailable, 503),
    (AuthError, 502),
    (ProviderError, 502),
    (AIBackendError, 502),
    (PluginError, 502),
    (ConfigError, 500),
    (StorageError, 500),
)


class ApiError(Exception):
    """A problem with the request itself, found before core is asked anything."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def status_for(exc: OakenError) -> int:
    for cls, status in STATUS:
        if isinstance(exc, cls):
            return status
    return 500


def body(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


INTERNAL = body("internal", "something went wrong inside core; see its log")
