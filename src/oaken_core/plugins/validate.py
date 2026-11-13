# SPDX-License-Identifier: Apache-2.0
"""Check a plugin against its kind's contract. Used by the host at load time and by the
conformance suites.

Adapted from oaken-mail ``plugins/validate.py`` (cf3b187). Returns a list of problems in plain
words rather than raising on the first one, so a plugin author sees everything to fix at once.
"""

import re

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import BaseModel

from oaken_core.plugins.base import is_implemented
from oaken_core.plugins.contracts import (
    PLUGIN_API_VERSION,
    PluginInfo,
    PluginJobHandler,
    PluginOperation,
    PluginScheduledTask,
)
from oaken_core.plugins.kinds import KindSpec

_OP_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
NEED = re.compile(r"^([a-z][a-z0-9_-]*)@([a-z][a-z0-9_]*)$")

# Values a plugin may only learn in ``start()`` (the model it was configured with), so the host
# checks them after starting and not on the fresh instance.
STARTED_VALUES = frozenset({"model_id", "dimensions", "context_window"})


def check_plugin(
    kind: KindSpec, name: str, obj: object, core_version: str, *, defer_started: bool = False
) -> tuple[PluginInfo | None, list[str]]:
    """The ``PluginInfo`` for ``obj`` (None if it cannot be built) and every problem found.

    With ``defer_started`` the ``STARTED_VALUES`` are only checked to exist; ``check_started``
    checks what they hold once the plugin has started."""
    problems: list[str] = []
    version = getattr(obj, "api_version", PLUGIN_API_VERSION)
    if version != PLUGIN_API_VERSION:
        problems.append(f"api_version is {version!r}, this core speaks {PLUGIN_API_VERSION}")
    problems.extend(check_requires_core(obj, core_version))
    for member in kind.required:
        if not hasattr(obj, member):
            problems.append(f"missing {member}")
        elif callable(getattr(obj, member)) is False:
            if not (defer_started and member in STARTED_VALUES):
                problems.extend(_check_value(obj, member))
        elif not is_implemented(obj, member):
            problems.append(f"{member}() is not implemented")
    problems.extend(check_operations(obj))
    problems.extend(check_job_handlers(obj))
    problems.extend(check_scheduled_tasks(obj))
    if kind.analyzer:
        problems.extend(check_needs(obj))
        vec = getattr(obj, "needs_vec", False)
        if not isinstance(vec, bool):
            problems.append("needs_vec must be True or False")
    info = PluginInfo(
        name=name, version=str(getattr(obj, "version", "") or ""), kind=kind.name,
        api_version=PLUGIN_API_VERSION,
    )  # fmt: skip
    return (info if not problems else None), problems


def check_requires_core(obj: object, core_version: str) -> list[str]:
    spec = str(getattr(obj, "requires_core", "") or "")
    if not spec:
        return []
    try:
        ok = SpecifierSet(spec).contains(core_version, prereleases=True)
    except InvalidSpecifier:
        return [f"requires_core {spec!r} is not a version range"]
    return [] if ok else [f"needs oaken-core {spec}, this is {core_version}"]


def check_needs(obj: object) -> list[str]:
    needs = getattr(obj, "needs", ())
    if not isinstance(needs, tuple | list) or not all(isinstance(n, str) for n in needs):
        return ["needs must be a list of '<analyzer>@<relation>' strings"]
    return [f"need {n!r} must look like '<analyzer>@<relation>'" for n in needs if not NEED.match(n)]


def check_started(kind: KindSpec, obj: object) -> list[str]:
    """Problems with the ``STARTED_VALUES`` of a plugin that has just started."""
    problems: list[str] = []
    for member in kind.required:
        if member in STARTED_VALUES and hasattr(obj, member):
            problems.extend(_check_value(obj, member))
    return problems


def _check_value(obj: object, member: str) -> list[str]:
    v = getattr(obj, member)
    if member in ("model_id", "version"):
        return [] if isinstance(v, str) and v else [f"{member} must be a non-empty string"]
    if member in ("dimensions", "context_window"):
        ok = isinstance(v, int) and not isinstance(v, bool) and v > 0
        return [] if ok else [f"{member} must be a positive integer"]
    if member == "needs_llm":
        return [] if isinstance(v, bool) else ["needs_llm must be True or False"]
    return []


def declared_operations(obj: object) -> list[PluginOperation]:
    fn = getattr(obj, "operations", None)
    return list(fn()) if callable(fn) else []


def check_operations(obj: object) -> list[str]:
    try:
        ops = declared_operations(obj)
    except Exception as exc:  # noqa: BLE001 - a plugin that cannot say what it offers is a broken plugin
        return [f"operations() failed: {type(exc).__name__}"]
    problems: list[str] = []
    seen: set[str] = set()
    for op in ops:
        if not isinstance(op, PluginOperation):
            problems.append("operations() must return PluginOperation values")
            continue
        label = f"operation {op.name!r}"
        if not _OP_NAME.match(op.name):
            problems.append(f"{label}: a name is lower-case letters, digits and underscores")
        if op.name in seen:
            problems.append(f"{label} is defined twice")
        seen.add(op.name)
        if op.permission not in ("read", "mutate"):
            problems.append(f"{label}: permission is read or mutate")
        if op.mcp not in ("off", "never"):
            problems.append(f"{label}: mcp is off or never (never on by default)")
        for side, model in (("input", op.input), ("output", op.output)):
            if not (isinstance(model, type) and issubclass(model, BaseModel)):
                problems.append(f"{label}: {side} must be a pydantic model class")
        if not callable(op.handler):
            problems.append(f"{label}: handler must be an async function")
    return problems


def declared_job_handlers(obj: object) -> list[PluginJobHandler]:
    fn = getattr(obj, "job_handlers", None)
    return list(fn()) if callable(fn) else []


def check_job_handlers(obj: object) -> list[str]:
    try:
        handlers = declared_job_handlers(obj)
    except Exception as exc:  # noqa: BLE001 - plugin code can raise anything; only the type is kept
        return [f"job_handlers() failed: {type(exc).__name__}"]
    problems: list[str] = []
    seen: set[str] = set()
    for jh in handlers:
        if not isinstance(jh, PluginJobHandler):
            problems.append("job_handlers() must return PluginJobHandler values")
            continue
        label = f"job kind {jh.kind!r}"
        if not _OP_NAME.match(jh.kind):
            problems.append(f"{label}: a kind is lower-case letters, digits and underscores")
        if jh.kind in seen:
            problems.append(f"{label} is defined twice")
        seen.add(jh.kind)
        if not (isinstance(jh.payload_model, type) and issubclass(jh.payload_model, BaseModel)):
            problems.append(f"{label}: payload_model must be a pydantic model class")
        if jh.result_model is not None and not (
            isinstance(jh.result_model, type) and issubclass(jh.result_model, BaseModel)
        ):
            problems.append(f"{label}: result_model must be a pydantic model class")
        if not callable(jh.handler):
            problems.append(f"{label}: handler must be an async function")
        if jh.concurrency is not None and (not isinstance(jh.concurrency, int) or jh.concurrency < 1):
            problems.append(f"{label}: concurrency must be a positive integer")
    return problems


def declared_scheduled_tasks(obj: object) -> list[PluginScheduledTask]:
    fn = getattr(obj, "scheduled_tasks", None)
    return list(fn()) if callable(fn) else []


def check_scheduled_tasks(obj: object) -> list[str]:
    try:
        tasks = declared_scheduled_tasks(obj)
    except Exception as exc:  # noqa: BLE001 - plugin code can raise anything; only the type is kept
        return [f"scheduled_tasks() failed: {type(exc).__name__}"]
    problems: list[str] = []
    seen: set[str] = set()
    for task in tasks:
        if not isinstance(task, PluginScheduledTask):
            problems.append("scheduled_tasks() must return PluginScheduledTask values")
            continue
        label = f"scheduled task {task.name!r}"
        if not _OP_NAME.match(task.name):
            problems.append(f"{label}: a name is lower-case letters, digits and underscores")
        if task.name in seen:
            problems.append(f"{label} is defined twice")
        seen.add(task.name)
        if not (isinstance(task.job_kind, str) and task.job_kind):
            problems.append(f"{label}: job_kind must be a non-empty string")
        if not isinstance(task.payload, BaseModel):
            problems.append(f"{label}: payload must be a pydantic model instance")
        if (task.every is None) == (task.cron is None):
            problems.append(f"{label}: give exactly one of every= (seconds) or cron=")
        if task.every is not None and task.every <= 0:
            problems.append(f"{label}: every must be a positive number of seconds")
    return problems
