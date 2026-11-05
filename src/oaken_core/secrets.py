# SPDX-License-Identifier: Apache-2.0
"""Where credentials live: the OS keyring, never ``config.toml``.

A plugin reaches only its own entries through ``ctx.secrets`` (``get``, ``set``, ``delete``). Each is
filed under the product's app name and the plugin's ``<kind>:<name>``, so two products, or two
instances of one plugin, never see each other's. Secrets are never logged, traced or put in an
operation's audit row (an input that carries one is a ``SecretStr``).
"""

from typing import Any, Protocol

from oaken_core import identity
from oaken_core.errors import PluginError


class SecretStore(Protocol):
    def get(self, owner: str, key: str) -> str | None: ...
    def set(self, owner: str, key: str, value: str) -> None: ...
    def delete(self, owner: str, key: str) -> bool: ...


class KeyringSecrets:
    """The OS keyring (``keyring``): Secret Service, Keychain or Credential Locker. The service name is
    the product's app, the user name ``<owner>/<key>``. Imported when first used, so core runs without
    a keyring until something stores a secret."""

    def __init__(self, service: str | None = None) -> None:
        self._service = service

    @property
    def service(self) -> str:
        return self._service or identity.app

    def _keyring(self) -> Any:
        try:
            import keyring
        except ImportError:
            raise PluginError("no keyring is installed (pip install keyring); secrets need one") from None
        return keyring

    def _call(self, fn: str, *args: str) -> Any:
        kr = self._keyring()
        try:
            return getattr(kr, fn)(self.service, *args)
        except kr.errors.PasswordDeleteError:
            return None
        except kr.errors.KeyringError as exc:  # no backend, locked, denied: say which, never the value
            raise PluginError(f"the OS keyring failed: {type(exc).__name__}") from None

    def get(self, owner: str, key: str) -> str | None:
        value: str | None = self._call("get_password", f"{owner}/{key}")
        return value

    def set(self, owner: str, key: str, value: str) -> None:
        self._call("set_password", f"{owner}/{key}", value)

    def delete(self, owner: str, key: str) -> bool:
        kr = self._keyring()
        try:
            kr.delete_password(self.service, f"{owner}/{key}")
        except kr.errors.PasswordDeleteError:
            return False
        except kr.errors.KeyringError as exc:
            raise PluginError(f"the OS keyring failed: {type(exc).__name__}") from None
        return True


class MemorySecrets:
    """For tests and for a machine with no keyring."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], str] = {}

    def get(self, owner: str, key: str) -> str | None:
        return self._items.get((owner, key))

    def set(self, owner: str, key: str, value: str) -> None:
        self._items[(owner, key)] = value

    def delete(self, owner: str, key: str) -> bool:
        return self._items.pop((owner, key), None) is not None


class PluginSecrets:
    """One plugin's view of the store: its own ``<kind>:<name>`` and nothing else."""

    def __init__(self, store: SecretStore, kind: str, name: str) -> None:
        self._store, self.owner = store, f"{kind}:{name}"

    def get(self, key: str) -> str | None:
        return self._store.get(self.owner, key)

    def set(self, key: str, value: str) -> None:
        if not key or not value:
            raise PluginError("a secret needs a name and a value")
        self._store.set(self.owner, key, value)

    def delete(self, key: str) -> bool:
        return self._store.delete(self.owner, key)
