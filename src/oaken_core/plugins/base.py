# SPDX-License-Identifier: Apache-2.0
"""Optional base classes that make writing a plugin short.

Adapted from oaken-mail ``plugins/base.py`` (cf3b187): ``BasePlugin`` and the ``@command``
decorator. Nothing requires these: any object with the members of a contract works, and the host
checks the same rules either way (``oaken_core.plugins.validate``).

    class Hello(BasePlugin):
        @command("hi", "Say hi.", input=HiIn, output=HiOut)
        async def hi(self, ctx: PluginOpContext, inp: HiIn) -> HiOut: ...

A ``@command`` becomes an operation ``<plugin>.<name>``: it is in the operation registry, so the
CLI builds a verb for it (``<product> <plugin> <name>``, one flag per input field) at the moment the
host loads the plugin, and a plugin that fails to load takes its commands with it.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, Literal, TypeVar

from pydantic import BaseModel, ValidationError

from oaken_core.db import Database
from oaken_core.errors import PluginError
from oaken_core.labels import BaseLabel
from oaken_core.plugins.contracts import (
    PLUGIN_API_VERSION,
    Health,
    PluginContext,
    PluginJobHandler,
    PluginOperation,
    PluginScheduledTask,
    UiSpec,
)

F = TypeVar("F", bound=Callable[..., Any])


def _default(fn: F) -> F:
    """Mark a base-class method as a placeholder: overriding it is what implements it."""
    fn._oaken_default = True  # type: ignore[attr-defined]
    return fn


def is_implemented(obj: object, method: str) -> bool:
    """True if ``obj`` has a callable ``method`` that is not a base-class placeholder."""
    fn = getattr(obj, method, None)
    return callable(fn) and not getattr(fn, "_oaken_default", False)


def command(
    name: str,
    summary: str,
    *,
    input: type[BaseModel],
    output: type[BaseModel],
    permission: Literal["read", "mutate"] = "read",
    preview: str | None = None,
    mcp: Literal["off", "never"] = "off",
) -> Callable[[F], F]:
    """Offer a method of your plugin as a command: an operation core registers as
    ``<plugin>.<name>``. ``preview`` names another method (same signature, returning
    ``PluginPreview``) that says what a ``mutate`` command would change. A command is never on over
    MCP until the user turns it on."""

    def wrap(fn: F) -> F:
        fn._oaken_command = {  # type: ignore[attr-defined]
            "name": name, "summary": summary, "input": input, "output": output,
            "permission": permission, "preview": preview, "mcp": mcp,
        }  # fmt: skip
        return fn

    return wrap


class BasePlugin:
    api_version: ClassVar[int] = PLUGIN_API_VERSION
    version: ClassVar[str] = ""
    # A version range of Oaken Core this plugin works with (``">=0.1,<0.2"``). Empty: any. The
    # host refuses a plugin outside it.
    requires_core: ClassVar[str] = ""
    # Shown where people set the plugin up: a few plain sentences and a config snippet.
    setup_help: ClassVar[str] = ""
    config_example: ClassVar[str] = ""
    # A pydantic model of this plugin's settings. Declare it and ``start`` parses the table into
    # ``self.config`` (typed, validated), and the host publishes its JSON Schema so the console can
    # render a settings form. Leave it None and the plugin reads ``ctx.config`` (a dict) itself.
    config_model: ClassVar[type[BaseModel] | None] = None
    # Whether the plugin runs when its table does not say ``enabled``. A plugin that should be
    # opt-in (it costs money, or reaches a network) sets this False.
    enabled_default: ClassVar[bool] = True
    # Settings the plugin cannot run without. While its table lacks any, the host leaves it
    # switched off with the reason shown, and starts it when they are set.
    requires_config: ClassVar[tuple[str, ...]] = ()
    # A folder of ``NNNN_name.sql`` migrations for tables the plugin owns, applied by the host (in
    # the same run as core's) before ``start``. Name the tables ``<plugin>_...``.
    migrations_dir: ClassVar[Path | None] = None
    # For an analyzer: the stored results it reads, ``"<analyzer>@<relation>"``. Must be acyclic.
    needs: ClassVar[tuple[str, ...]] = ()

    ctx: PluginContext  # set by start()
    config: Any = None  # the parsed config_model instance, set by start() when config_model is set

    def labels(self) -> list[BaseLabel]:
        """The label keys this plugin defines. Registered before ``start``; a clash with a
        reserved key raises ``LabelError`` and the plugin is switched off."""
        return []

    def ui(self) -> UiSpec | None:
        """The plugin's UI as data: settings sections, buttons and pages. None: the settings
        modal is built from the config schema alone."""
        return None

    def job_handlers(self) -> list[PluginJobHandler]:
        return []

    def scheduled_tasks(self) -> list[PluginScheduledTask]:
        return []

    def trace(self, kind: str, /, **data: Any) -> None:
        """Record an event of your own in the trace. Does nothing unless tracing is on."""
        ctx = getattr(self, "ctx", None)
        if ctx is not None:
            ctx.trace(kind, **data)

    async def start(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        if self.config_model is not None:
            try:
                self.config = self.config_model.model_validate(ctx.config)
            except ValidationError as exc:
                fields = ", ".join(str(e["loc"][0]) for e in exc.errors() if e["loc"])
                raise PluginError(
                    f"{ctx.name}: invalid [plugins.{ctx.kind}.{ctx.name}] settings ({fields})"
                ) from None

    async def stop(self) -> None:
        return None

    async def health(self) -> Health:
        return Health(True)

    def operations(self) -> list[PluginOperation]:
        """The methods marked with ``@command``. Override to add operations by hand, and call
        ``super().operations()`` to keep these."""
        found: list[PluginOperation] = []
        for attr in sorted(dir(type(self))):
            spec = getattr(getattr(type(self), attr, None), "_oaken_command", None)
            if spec is None:
                continue
            preview = getattr(self, spec["preview"]) if spec["preview"] else None
            found.append(
                PluginOperation(
                    name=spec["name"], summary=spec["summary"], permission=spec["permission"],
                    input=spec["input"], output=spec["output"], handler=getattr(self, attr),
                    preview=preview, mcp=spec["mcp"],
                )  # fmt: skip
            )
        return found


class BaseStore(BasePlugin):
    """A store hands core a ``Database``. Core and plugins write portable SQL against it."""

    @_default
    async def open(self) -> Database:
        raise NotImplementedError("a store must implement open()")


class BaseTriager(BasePlugin):
    """Rates an intake item before it is stored. Lowest ``rank`` first; the first verdict wins."""

    version: ClassVar[str] = "1"
    needs_llm: ClassVar[bool] = False
    rank: ClassVar[int] = 50

    @_default
    async def triage(self, item: Any) -> Any:
        raise NotImplementedError("a triager must implement triage()")


class BaseEnricher(BasePlugin):
    version: ClassVar[str] = "1"
    needs_llm: ClassVar[bool] = False

    @_default
    async def enrich(self, target: Any) -> Any:
        raise NotImplementedError("an enricher must implement enrich()")


class BaseAction(BasePlugin):
    version: ClassVar[str] = "1"
    needs_llm: ClassVar[bool] = False
    routes: ClassVar[tuple[str, ...]] = ()

    @_default
    async def act(self, target: Any) -> Any:
        raise NotImplementedError("an action must implement act()")
