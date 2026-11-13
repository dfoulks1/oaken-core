# SPDX-License-Identifier: Apache-2.0
"""Turn what plugins offer into registry operations.

A plugin declares ``PluginOperation`` values; this namespaces each as ``<plugin>.<name>`` and wraps
it so the same rules apply as to a core operation. The plugin never sees the database and cannot be on over MCP until the user turns it on. A plugin's own errors are
reduced to their type: a failure inside a plugin can quote the private text it was handling.
"""

import asyncio
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from oaken_core.errors import OakenError, PluginError
from oaken_core.logging import get_logger
from oaken_core.operations.context import OpContext
from oaken_core.operations.registry import (
    PLUGIN_SEP,
    AnyOperation,
    Change,
    Operation,
    OperationDefinitionError,
    Preview,
)
from oaken_core.plugins.host import OfferedOperation, PluginHost

log = get_logger("oaken_core.operations")


def adapt(host: PluginHost, offered: OfferedOperation) -> AnyOperation:
    plugin, d = offered.plugin, offered.declaration
    timeout = host.call_timeout

    def pctx(ctx: OpContext) -> Any:
        return host.operation_context(
            plugin, ctx.caller.client, ctx.caller.actor.label or ctx.caller.actor.kind
        )

    async def handler(ctx: OpContext, inp: BaseModel) -> BaseModel:
        try:
            result = await asyncio.wait_for(d.handler(pctx(ctx), inp), timeout)
            return d.output.model_validate(result)
        except OakenError:
            raise
        except Exception as exc:  # noqa: BLE001 - plugin code can raise anything; only the type is kept
            raise PluginError(f"{plugin}.{d.name} failed ({type(exc).__name__})") from None

    async def preview(ctx: OpContext, inp: BaseModel) -> Preview:
        if d.preview is None:
            return Preview(summary=f"Run {plugin}.{d.name}: {d.summary}")
        try:
            p = await asyncio.wait_for(d.preview(pctx(ctx), inp), timeout)
        except OakenError:
            raise
        except Exception as exc:  # noqa: BLE001 - plugin code can raise anything; only the type is kept
            raise PluginError(f"{plugin}.{d.name} preview failed ({type(exc).__name__})") from None
        return Preview(
            summary=p.summary,
            changes=[
                Change(target=c.target, field=c.field, before=c.before, after=c.after)
                for c in p.changes
            ],
            total_changes=p.total,
        )

    return Operation(
        name=f"{plugin}{PLUGIN_SEP}{d.name}",
        summary=d.summary,
        permission=d.permission,
        input=d.input,
        output=d.output,
        handler=handler,
        mcp=d.mcp,
        preview=preview if d.permission == "mutate" else None,
        audit="registry",
        plugin=plugin,
        template=offered.template,
        description=f"Offered by the {plugin} plugin.",
    )


def plugin_operations(host: PluginHost) -> Sequence[AnyOperation]:
    """The operations the healthy plugins offer now. One that breaks a rule is left out (logged)."""
    out: list[AnyOperation] = []
    for offered in host.offered_operations():
        try:
            out.append(adapt(host, offered))
        except OperationDefinitionError as exc:
            log.error("plugin operation refused", plugin=offered.plugin, problem=str(exc))
    return out
