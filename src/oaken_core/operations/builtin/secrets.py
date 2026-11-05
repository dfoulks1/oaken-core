# SPDX-License-Identifier: Apache-2.0
"""Store, check and remove a plugin's credentials in the OS keyring. The value is a ``SecretStr``: the
CLI asks for it without echo, and it is masked wherever the input is dumped, so it reaches neither the
audit log nor a trace. Never offered over MCP."""

from pydantic import BaseModel, Field, SecretStr

from oaken_core.errors import InvalidInput, NotFound, Unavailable
from oaken_core.operations.context import OpContext
from oaken_core.operations.registry import Change, Preview, operation
from oaken_core.secrets import PluginSecrets


class SecretRef(BaseModel):
    kind: str = Field(description="The plugin's kind, for example `provider`.")
    name: str = Field(description="The plugin instance's name, as `plugin list` shows it.")
    key: str = Field(description="What the secret is called, for example `password`.")


class SecretSetIn(SecretRef):
    value: SecretStr = Field(description="The secret. Asked for without echo; never taken as an argument.")


class SecretStatusOut(BaseModel):
    kind: str
    name: str
    key: str
    stored: bool


def _store(ctx: OpContext, inp: SecretRef) -> PluginSecrets:
    if ctx.svc.secrets is None:
        raise Unavailable("this core has no keyring")
    return PluginSecrets(ctx.svc.secrets, inp.kind, inp.name)


def _known(ctx: OpContext, inp: SecretRef) -> None:
    host = ctx.svc.host
    if host is not None and not any((s.kind, s.name) == (inp.kind, inp.name) for s in host.statuses()):
        raise NotFound(f"no {inp.kind} plugin named {inp.name!r}")


async def _preview_set(ctx: OpContext, inp: SecretSetIn) -> Preview:
    return Preview(summary=f"Store {inp.key} for {inp.kind} {inp.name} in the OS keyring",
                   changes=[Change(target=f"{inp.kind}:{inp.name}", field=inp.key)], total_changes=1)  # fmt: skip


@operation("secret_set", "Store a plugin's credential in the OS keyring (never in config.toml).",
           permission="mutate", input=SecretSetIn, output=SecretStatusOut, mcp="never", group="secret",
           preview=_preview_set)  # fmt: skip
async def secret_set(ctx: OpContext, inp: SecretSetIn) -> SecretStatusOut:
    value = inp.value.get_secret_value()
    if not value:
        raise InvalidInput("no secret given")
    _known(ctx, inp)
    _store(ctx, inp).set(inp.key, value)
    return SecretStatusOut(kind=inp.kind, name=inp.name, key=inp.key, stored=True)


async def _preview_delete(ctx: OpContext, inp: SecretRef) -> Preview:
    return Preview(summary=f"Remove {inp.key} of {inp.kind} {inp.name} from the OS keyring",
                   changes=[Change(target=f"{inp.kind}:{inp.name}", field=inp.key)], total_changes=1)  # fmt: skip


@operation("secret_delete", "Remove a plugin's credential from the OS keyring.",
           permission="mutate", input=SecretRef, output=SecretStatusOut, mcp="never", group="secret",
           preview=_preview_delete)  # fmt: skip
async def secret_delete(ctx: OpContext, inp: SecretRef) -> SecretStatusOut:
    _store(ctx, inp).delete(inp.key)
    return SecretStatusOut(kind=inp.kind, name=inp.name, key=inp.key, stored=False)


@operation("secret_status", "Whether a plugin's credential is stored. Never shows it.",
           permission="read", input=SecretRef, output=SecretStatusOut, mcp="never", group="secret")  # fmt: skip
async def secret_status(ctx: OpContext, inp: SecretRef) -> SecretStatusOut:
    return SecretStatusOut(kind=inp.kind, name=inp.name, key=inp.key, stored=_store(ctx, inp).get(inp.key) is not None)


OPERATIONS = (secret_set, secret_delete, secret_status)
