# SPDX-License-Identifier: Apache-2.0
import asyncio
import os
import signal
from pathlib import Path

import httpx2
import pytest

from oaken_core import paths
from oaken_core.api.runtime import RuntimeFile, read_runtime
from oaken_core.clock import SystemClock
from oaken_core.daemon import lifecycle
from oaken_core.daemon.core import Core
from oaken_core.daemon.units import Lifecycle, unit
from oaken_core.errors import ConfigError, Conflict, Unavailable
from oaken_core.settings import Settings


# -- units -------------------------------------------------------------------------------------
async def test_units_start_in_order_stop_in_reverse_and_a_failed_start_unwinds() -> None:
    seen: list[str] = []

    def mk(name: str, fail: bool = False) -> object:
        async def start() -> None:
            if fail:
                raise RuntimeError(name)
            seen.append(f"+{name}")

        async def stop() -> None:
            seen.append(f"-{name}")

        return unit(name, start, stop)

    started: list[str] = []
    ok = Lifecycle([mk("a"), mk("b")])  # type: ignore[list-item]
    await ok.start(started)
    await ok.stop(started)
    assert seen == ["+a", "+b", "-b", "-a"] and started == []
    seen.clear()
    bad = Lifecycle([mk("a"), mk("b", fail=True), mk("c")])  # type: ignore[list-item]
    with pytest.raises(RuntimeError, match="b"):
        await bad.start(started)
    assert seen == ["+a", "-a"] and started == []


# -- settings ----------------------------------------------------------------------------------
def test_settings_load_maps_plugin_tables_active_and_mcp_allowed(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[jobs]\nmax_attempts = 7\n[active]\nstore = "sqlite"\n[mcp.allowed]\nstatus = true\n'
                   '[plugins.llm_backend.desk]\nplugin = "ollama"\nhost = "h"\n')  # fmt: skip
    s = Settings.load(cfg)
    assert s.jobs.max_attempts == 7 and s.plugins.active == {"store": "sqlite"}
    assert s.operations.mcp_allowed == {"status": True}
    assert s.plugins.instances("llm_backend") == {"desk": "ollama"}
    assert Settings.load(tmp_path / "absent.toml") == Settings()


def test_settings_load_names_the_file_on_a_bad_value_or_syntax(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("[jobs]\nmax_attempts = 0\n")
    with pytest.raises(ConfigError, match="config.toml"):
        Settings.load(cfg)
    cfg.write_text("[jobs\n")
    with pytest.raises(ConfigError, match="cannot read"):
        Settings.load(cfg)


# -- running -----------------------------------------------------------------------------------
async def health(core: Core) -> dict[str, str]:
    token = (paths.config_dir() / "api_token").read_text().strip()
    async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{core.port}", headers={"authorization": f"Bearer {token}"}) as c:
        ops = (await c.get("/v1/operations")).json()["operations"]
        assert "status" in {o["name"] for o in ops}
        return (await c.get("/v1/health")).json()


async def test_core_comes_up_serves_and_goes_down_in_reverse() -> None:
    core = Core()
    await core.open()
    await core.start()
    assert core.started == ["trace", "console", "host", "scheduler", "jobs", "mcp", "api"] and core.port and core.console_port
    assert await health(core) == {"status": "ok"}
    await core.stop()
    assert core.started == [] and core.host.db is None


async def test_a_taken_port_fails_start_and_leaves_nothing_open() -> None:
    first = Core()
    await first.open()
    await first.start()
    try:
        second = Core(Settings(console={"port": 0}, api={"port": first.port}))
        await second.open()
        with pytest.raises(Unavailable, match="could not listen"):
            await second.start()
        assert second.started == [] and second.host.db is None
    finally:
        await first.stop()


async def test_serve_writes_the_runtime_file_refuses_a_second_and_stops_on_the_event() -> None:
    stop, ready = asyncio.Event(), asyncio.Event()
    runtime = RuntimeFile(paths.runtime_file(), SystemClock(), "t")
    task = asyncio.create_task(lifecycle.serve(Settings(console={"port": 0}), runtime=runtime, stop=stop, ready=ready, handle_signals=False))
    await asyncio.wait_for(ready.wait(), 20)
    data = read_runtime(runtime.path)
    assert data and data["pid"] == os.getpid() and data["port"] > 0
    assert lifecycle.status() == data
    other = RuntimeFile(runtime.path, SystemClock(), "t")
    other.pid = os.getppid()  # the lock refuses a different live process (same pid means ours)
    with pytest.raises(Conflict):
        other.claim()
    stop.set()
    await asyncio.wait_for(task, 20)
    assert not runtime.path.exists() and lifecycle.status() is None


async def test_reload_rebuilds_on_the_new_settings_keeps_the_port_and_refuses_a_bad_file() -> None:
    stop, ready, reload = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cfg = [Settings(console={"port": 0})]

    def load() -> Settings:
        if cfg[0] is None:
            raise ConfigError("bad file")
        return cfg[0]

    runtime = RuntimeFile(paths.runtime_file(), SystemClock(), "t")
    task = asyncio.create_task(lifecycle.serve(Settings(console={"port": 0}), runtime=runtime, stop=stop, ready=ready,
                                               reload=reload, load_settings=load, handle_signals=False))  # fmt: skip
    await asyncio.wait_for(ready.wait(), 20)
    port, console = (read_runtime(runtime.path) or {})["port"], (read_runtime(runtime.path) or {})["console_port"]

    async def reloaded() -> None:
        reload.set()
        while reload.is_set():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.5)

    cfg[0] = Settings(console={"port": 0}, jobs={"max_attempts": 9})
    await reloaded()
    assert (read_runtime(runtime.path) or {})["port"] == port
    assert (read_runtime(runtime.path) or {})["console_port"] == console  # the page open in a browser stays put
    cfg[0] = None  # type: ignore[call-overload]
    await reloaded()
    assert (read_runtime(runtime.path) or {})["port"] == port  # still up on the old configuration
    stop.set()
    await asyncio.wait_for(task, 20)


def test_stop_and_reload_with_no_core_running_say_so() -> None:
    assert not lifecycle.reload() and not lifecycle.stop_gracefully().running
    assert signal.SIGHUP  # the reload signal exists here


async def test_the_core_reload_operation_proposes_then_rebuilds_core_on_the_file() -> None:
    from oaken_core.operations.caller import CallerContext

    stop, ready = asyncio.Event(), asyncio.Event()
    cfg, made = [Settings(console={"port": 0})], []

    def make(settings: Settings) -> Core:
        made.append(Core(settings))
        return made[-1]

    runtime = RuntimeFile(paths.runtime_file(), SystemClock(), "t")
    task = asyncio.create_task(lifecycle.serve(Settings(console={"port": 0}), make=make, runtime=runtime, stop=stop, ready=ready,
                                               load_settings=lambda: cfg[0], handle_signals=False))  # fmt: skip
    try:
        await asyncio.wait_for(ready.wait(), 20)
        cli, first = CallerContext.cli(), made[-1]
        console = first.console_port  # read now: a stopped core's port reads 0
        proposal = await first.client.call("core_reload", {}, cli)
        assert proposal.proposal is not None and len(made) == 1  # a proposal changes nothing
        cfg[0] = Settings(console={"port": 0}, jobs={"max_attempts": 9})
        done = await first.client.call("core_reload", {}, cli, confirm=True)
        assert done.output.reloading  # type: ignore[union-attr]
        for _ in range(100):
            if len(made) == 2 and made[-1].started:
                break
            await asyncio.sleep(0.05)
        assert len(made) == 2 and made[-1].settings.jobs.max_attempts == 9
        assert made[-1].console_port == console  # the same console, after the reload
    finally:
        stop.set()
        await asyncio.wait_for(task, 20)


async def test_core_reload_says_so_when_core_was_not_started_by_the_daemon() -> None:
    from oaken_core.errors import Unavailable
    from oaken_core.operations.caller import CallerContext

    core = Core(Settings(console={"port": 0}))
    await core.open()
    try:
        with pytest.raises(Unavailable, match="nothing to reload"):
            await core.client.call("core_reload", {}, CallerContext.cli(), confirm=True)
    finally:
        await core.host.stop()
