# SPDX-License-Identifier: Apache-2.0
"""Error taxonomy. Every error carries a stable machine ``code`` that clients can match on."""


class OakenError(Exception):
    """Base class for every error Oaken raises on purpose."""

    code = "oaken_error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)
        self.message = message or self.code

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class ConfigError(OakenError):
    code = "config_error"


class StorageError(OakenError):
    code = "storage_error"


class IndexMismatch(StorageError):
    """``index.db`` was built from a different raw store, or by an unknown format. It is refused,
    never repaired in place: delete it and rebuild."""

    code = "index_mismatch"


class PluginError(OakenError):
    code = "plugin_error"


class ProviderError(OakenError):
    code = "provider_error"


class AuthError(ProviderError):
    """The service refused our credentials, or has none to give. Retrying cannot fix it: the user
    must act (re-authorize, store a new password). Core pauses the account's sync on this."""

    code = "auth_required"


class AIBackendError(OakenError):
    code = "ai_backend_error"


class PermissionDenied(OakenError):
    code = "permission_denied"


class NotFound(OakenError):
    code = "not_found"


class Conflict(OakenError):
    code = "conflict"


class QuotaExceeded(OakenError):
    code = "quota_exceeded"


class InvalidResult(OakenError):
    """A job result that fails its schema or size limit. The job is left leased."""

    code = "invalid_result"


class InvalidInput(OakenError):
    """An operation was called with input that does not fit its model, or breaks a limit."""

    code = "invalid_input"


class Unavailable(OakenError):
    """A service the operation needs is not running in this process."""

    code = "unavailable"


class ConfirmationInvalid(PermissionDenied):
    """A confirmation token that is unknown, used already, expired, or for a different call."""

    code = "confirmation_invalid"


class NotExposed(PermissionDenied):
    """The operation is not offered to this kind of caller (MCP exposure is the user's setting)."""

    code = "not_exposed"


class SendDisabled(PermissionDenied):
    """Sending is defined but switched off in v1."""

    code = "send_disabled"
