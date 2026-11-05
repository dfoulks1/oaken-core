# SPDX-License-Identifier: Apache-2.0
"""Running core as a process: one instance, a clean stop on a signal, reload on SIGHUP.

Lifted from oaken-mail's ``daemon/lifecycle.py`` (cf3b187); detach and restart are left out (run
under a service manager). The runtime file is the single-instance lock and how clients find the
port. SIGINT and SIGTERM stop core: it stops accepting, lets jobs finish or hands their leases
back, closes the database and removes the runtime file.
"""

import asyncio
import contextlib
import os
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oaken_core import __version__, paths
from oaken_core.api.runtime import RuntimeFile, pid_alive, read_runtime
from oaken_core.clock import SystemClock
from oaken_core.daemon.core import Core
from oaken_core.errors import ConfigError, Conflict, Unavailable
from oaken_core.logging import configure_logging, get_logger
from oaken_core.settings import Settings

log = get_logger("oaken_core.daemon")
MakeCore = Callable[[Settings], Core]


def _install(loop: asyncio.AbstractEventLoop, sigs: dict[int, Callable[[], None]]) -> list[int]:
    done: list[int] = []
    for sig, fn in sigs.items():
        try:
            loop.add_signal_handler(sig, fn)
        except (NotImplementedError, RuntimeError):  # not the main thread, or a loop without them
            continue
        done.append(sig)
    return done


async def _bring_up(core: Core, runtime: RuntimeFile) -> Core:
    await core.open()
    runtime.publish(await core.start(), core.console_port)
    return core


async def _reload(core: Core, runtime: RuntimeFile, make: MakeCore, load: Callable[[], Settings] | None) -> Core:
    """Rebuild core on the configuration file, in this process (same pid and runtime file). The
    file is validated before anything is stopped, so a typo changes nothing; if the rebuilt core
    cannot start, the old settings are put back."""
    if load is None:
        log.warning("reload ignored: core was not started from a configuration file")
        return core
    try:
        settings = load()
    except ConfigError as exc:
        log.error("reload refused; still running on the old configuration", error=str(exc))
        return core
    settings = settings.model_copy(update={"version": core.settings.version})
    if settings == core.settings:
        log.info("reload: configuration unchanged")
        return core
    configure_logging(settings.log)
    bound = core.port
    old = core.settings
    await core.stop()
    if settings.api.port == 0 and bound:  # no port asked for: keep the one clients already know
        settings = settings.model_copy(update={"api": settings.api.model_copy(update={"port": bound})})
    try:
        fresh = await _bring_up(make(settings), runtime)
    except Exception as exc:  # noqa: BLE001 - whatever it was, the old core comes back
        log.error("reload failed; restoring the old configuration", error=str(exc))
        configure_logging(old.log)
        fresh = await _bring_up(make(old), runtime)
    log.info("core reloaded", port=fresh.port)
    return fresh


async def serve(
    settings: Settings,
    *,
    make: MakeCore = Core,
    runtime: RuntimeFile | None = None,
    stop: asyncio.Event | None = None,
    ready: asyncio.Event | None = None,
    handle_signals: bool = True,
    reload: asyncio.Event | None = None,
    load_settings: Callable[[], Settings] | None = None,
) -> None:
    """Run until told to stop. ``Conflict`` at once if another core is running. Setting ``reload``
    (SIGHUP does) rebuilds core on ``load_settings()``."""
    runtime = runtime or RuntimeFile(paths.runtime_file(), SystemClock(), __version__)
    stop, reload = stop or asyncio.Event(), reload or asyncio.Event()
    runtime.claim()
    loop = asyncio.get_running_loop()
    sigs: dict[int, Callable[[], None]] = {signal.SIGINT: stop.set, signal.SIGTERM: stop.set}
    if hasattr(signal, "SIGHUP"):
        sigs[signal.SIGHUP] = reload.set
    installed = _install(loop, sigs) if handle_signals else []
    core: Core | None = None
    try:
        core = await _bring_up(make(settings), runtime)
        log.info("core running", port=core.port, console=f"http://127.0.0.1:{core.console_port}/", version=__version__)
        if ready is not None:
            ready.set()
        while not stop.is_set():
            waiters = {asyncio.ensure_future(stop.wait()), asyncio.ensure_future(reload.wait())}
            _, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if reload.is_set() and not stop.is_set():
                reload.clear()
                core = await _reload(core, runtime, make, load_settings)
        log.info("core stopping")
    finally:
        for sig in installed:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(sig)
        try:
            if core is not None:
                await core.stop()
        finally:
            runtime.release()


def run(settings: Settings | None = None) -> int:
    """Foreground entry point: 0 for a clean stop, 1 if core is already running or the
    configuration is wrong."""
    try:
        cfg = settings or Settings.load()
        configure_logging(cfg.log)
        paths.remember_home()
        asyncio.run(serve(cfg, load_settings=Settings.load if settings is None else None))
    except (Conflict, ConfigError) as exc:
        print(f"oaken: {exc}", file=sys.stderr)
        return 1
    return 0


# -- finding and stopping a running core -------------------------------------------------------
def status(runtime: Path | None = None) -> dict[str, Any] | None:
    """The runtime file's contents if that core is alive, else ``None``."""
    data = read_runtime(runtime or paths.runtime_file())
    pid = data.get("pid") if data else None
    return data if isinstance(pid, int) and pid_alive(pid) else None


DEFAULT_GRACE = 40.0  # core's own job grace (30s) and a margin to close its store


@dataclass(frozen=True)
class StopResult:
    running: bool  # core was running when asked to stop
    killed: bool = False  # it did not stop in time and was killed
    pid: int = 0


def _signal(found: dict[str, Any], sig: int, what: str) -> bool:
    try:
        os.kill(int(found["pid"]), sig)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise Unavailable(f"not allowed to {what} core (pid {found['pid']}): {exc}") from exc
    return True


def stop_gracefully(runtime: Path | None = None, *, grace: float = DEFAULT_GRACE, kill: bool = True) -> StopResult:
    """SIGTERM the running core and wait ``grace`` seconds; then kill it (``kill=False`` raises
    instead). The work is a durable queue with leases, so a killed core loses nothing."""
    found = status(runtime)
    if found is None or not _signal(found, signal.SIGTERM, "stop"):
        return StopResult(False)
    pid = int(found["pid"])
    deadline = time.monotonic() + grace
    while pid_alive(pid):
        if time.monotonic() > deadline:
            break
        time.sleep(0.05)
    else:
        return StopResult(True, pid=pid)
    if not kill:
        raise Unavailable(f"core (pid {pid}) did not stop within {grace:.0f}s")
    _signal(found, signal.SIGKILL, "kill")
    gone = time.monotonic() + 5.0
    while pid_alive(pid):
        if time.monotonic() > gone:
            raise Unavailable(f"core (pid {pid}) is still running after a kill")
        time.sleep(0.05)
    (runtime or paths.runtime_file()).unlink(missing_ok=True)  # a killed core cannot remove it
    return StopResult(True, killed=True, pid=pid)


def reload(runtime: Path | None = None) -> bool:
    """Ask the running core to re-read its configuration. ``False`` if none was running."""
    found = status(runtime)
    return found is not None and _signal(found, signal.SIGHUP, "signal")


def cli(argv: list[str]) -> int:
    """``start`` (foreground), ``stop`` and ``reload``: the verbs that work with no core running."""
    verb = argv[0]
    if verb == "start":
        return run()
    if verb == "stop":
        result = stop_gracefully()
        print("stopped" if result.running else "core is not running")
        return 0
    print("reloading" if reload() else "core is not running")
    return 0
