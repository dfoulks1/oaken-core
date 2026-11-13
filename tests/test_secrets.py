# SPDX-License-Identifier: Apache-2.0
"""Credentials: the keyring, one plugin's view of it, and the operations that set them."""

import sys
import types
from typing import Any

import pytest

from oaken_core import identity
from oaken_core.errors import InvalidInput, NotFound, PluginError
from oaken_core.operations.caller import CallerContext
from oaken_core.secrets import KeyringSecrets, MemorySecrets, PluginSecrets

from .conftest import Ops

CLI = CallerContext.cli()


class FakeKeyring(types.ModuleType):
    """Stands in for the ``keyring`` package, so the tests need no desktop keyring."""

    class errors:  # noqa: N801
        class KeyringError(Exception): ...

        class PasswordDeleteError(KeyringError): ...

    def __init__(self) -> None:
        super().__init__("keyring")
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, user: str) -> str | None:
        return self.items.get((service, user))

    def set_password(self, service: str, user: str, value: str) -> None:
        self.items[(service, user)] = value

    def delete_password(self, service: str, user: str) -> None:
        if self.items.pop((service, user), None) is None:
            raise self.errors.PasswordDeleteError("gone")


@pytest.fixture
def keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    fake = FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    return fake


def test_the_keyring_files_a_secret_under_the_product_and_the_plugin(keyring: FakeKeyring) -> None:
    identity.configure(app="oaken-grove")
    try:
        store = KeyringSecrets()
        store.set("provider:gmail", "password", "hunter2")
        assert keyring.items == {("oaken-grove", "provider:gmail/password"): "hunter2"}
        assert store.get("provider:gmail", "password") == "hunter2" and store.get("provider:other", "password") is None
        assert store.delete("provider:gmail", "password") is True and store.delete("provider:gmail", "password") is False
    finally:
        identity.configure()


def test_a_plugin_sees_only_its_own_entries() -> None:
    store = MemorySecrets()
    a, b = PluginSecrets(store, "provider", "a"), PluginSecrets(store, "provider", "b")
    a.set("password", "one")
    assert a.get("password") == "one" and b.get("password") is None
    with pytest.raises(PluginError):
        a.set("", "x")


def test_a_missing_keyring_is_a_plugin_error_that_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "keyring", None)  # an import of it now fails
    with pytest.raises(PluginError, match="keyring"):
        KeyringSecrets().get("provider:x", "k")


async def test_set_status_and_delete_never_show_or_log_the_value(ops: Ops) -> None:
    ops.svc.secrets = MemorySecrets()
    ops.svc.host = types.SimpleNamespace(statuses=lambda: [types.SimpleNamespace(kind="provider", name="gmail")])  # type: ignore[assignment]
    ref: dict[str, Any] = {"kind": "provider", "name": "gmail", "key": "password"}
    asked = await ops.client.call("secret_set", {**ref, "value": "s3cret"}, CLI)
    assert asked.proposal is not None and "s3cret" not in asked.proposal.model_dump_json()  # a proposal, no value in it
    done = await ops.client.call("secret_set", {**ref, "value": "s3cret"}, CLI, confirm=True)
    assert done.output.stored is True and "s3cret" not in done.output.model_dump_json()  # type: ignore[union-attr]
    assert (await ops.client.call("secret_status", ref, CLI)).output.stored is True  # type: ignore[union-attr]
    audit = " ".join(str(r) for r in await ops.svc.db.fetchall("SELECT * FROM change_log"))
    assert "s3cret" not in audit
    await ops.client.call("secret_delete", ref, CLI, confirm=True)
    assert (await ops.client.call("secret_status", ref, CLI)).output.stored is False  # type: ignore[union-attr]


async def test_an_unknown_plugin_or_an_empty_secret_is_refused(ops: Ops) -> None:
    ops.svc.secrets = MemorySecrets()
    ops.svc.host = types.SimpleNamespace(statuses=list)  # type: ignore[assignment]
    with pytest.raises(NotFound):
        await ops.client.call("secret_set", {"kind": "provider", "name": "nope", "key": "k", "value": "v"}, CLI, confirm=True)
    ops.svc.host = types.SimpleNamespace(statuses=lambda: [types.SimpleNamespace(kind="provider", name="gmail")])  # type: ignore[assignment]
    with pytest.raises(InvalidInput):
        await ops.client.call("secret_set", {"kind": "provider", "name": "gmail", "key": "k", "value": ""}, CLI, confirm=True)


async def test_secret_operations_are_never_offered_over_mcp(ops: Ops) -> None:
    mcp = {o.name for o in await ops.client.visible(CallerContext.mcp("me"))}
    assert not mcp & {"secret_set", "secret_delete", "secret_status"}


async def test_the_host_hands_each_plugin_its_own_secrets(tmp_path: Any) -> None:
    from oaken_core.clock import FakeClock

    from .test_host import STORE, make, stub

    seen: dict[str, Any] = {}

    class Keeper(stub("enricher")):  # type: ignore[misc]
        async def start(self, ctx: Any) -> None:
            await super().start(ctx)
            seen[ctx.name] = ctx.secrets

    host = make(tmp_path, FakeClock(), [STORE, ("enricher", "keeper", Keeper)])
    host.secrets = MemorySecrets()
    await host.start()
    try:
        seen["keeper"].set("token", "abc")
        assert host.secrets.get("enricher:keeper", "token") == "abc"  # filed under its own kind and name
    finally:
        await host.stop()
