# SPDX-License-Identifier: Apache-2.0
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from oaken_core.actors import SYSTEM, USER
from oaken_core.changelog import ChangeLog
from oaken_core.clock import FakeClock
from oaken_core.errors import NotFound, PluginError
from oaken_core.events import EventBus, PluginHealthChanged
from oaken_core.labels import BaseLabel, LabelError, LabelMaker, LabelStore
from oaken_core.plugins.base import BaseLLMAnalyzer, BasePlugin, BaseTriager, command
from oaken_core.plugins.contracts import Health, PluginOpContext
from oaken_core.plugins.host import PluginHost
from oaken_core.plugins.kinds import KindRegistry, KindSpec
from oaken_core.settings import PluginTables
from oaken_core.store import SqliteStore

STORE = ("store", "sqlite", SqliteStore)
VALUES: dict[str, Any] = {"version": "1", "needs_llm": False, "rank": 50, "model_id": "m", "dimensions": 3, "context_window": 8}


class EP:
    """An entry point, without installing anything."""

    def __init__(self, name: str, obj: Any) -> None:
        self.name, self._obj, self.dist = name, obj, None

    def load(self) -> Any:
        return self._obj


def make(tmp_path: Path, clock: FakeClock, plugins: list[tuple[str, str, Any]], tables: dict[str, Any] | None = None,
         bus: EventBus | None = None, kinds: KindRegistry | None = None) -> PluginHost:  # fmt: skip
    kinds = kinds or KindRegistry.shipped()
    groups = {k.name: k.group for k in kinds}
    found: dict[str, list[EP]] = {}
    for kind, name, obj in plugins:
        found.setdefault(groups[kind], []).append(EP(name, obj))
    cfg = {"store": {"sqlite": {"path": str(tmp_path / "core.db")}}, **(tables or {})}
    return PluginHost(PluginTables(tables=cfg), clock, bus, kinds=kinds, discover=lambda g: found.get(g, []), health_interval=0.01)


def stub(kind: str, **attrs: Any) -> type:
    """A minimal plugin of a shipped kind, with exactly the members its kind requires."""
    spec = KindRegistry.shipped().get(kind)

    async def method(self: Any, *a: Any, **k: Any) -> Any:
        return None

    body: dict[str, Any] = {m: (VALUES[m] if m in VALUES else method) for m in spec.required}
    return type(f"Stub_{kind}", (BasePlugin,), {**body, **attrs})


async def test_a_stub_of_every_shipped_kind_loads_starts_reports_health_and_stops(tmp_path: Path, clock: FakeClock) -> None:
    kinds = [k.name for k in KindRegistry.shipped() if k.name != "store"]
    host = make(tmp_path, clock, [STORE, *[(k, "stub", stub(k)) for k in kinds]])
    await host.start()
    try:
        by_kind = {s.kind: s for s in host.statuses()}
        assert set(by_kind) == {"store", *kinds}
        assert all(s.started and s.healthy for s in by_kind.values()), [s for s in by_kind.values() if not s.healthy]
    finally:
        await host.stop()
    assert all(not s.started for s in host.statuses())


async def test_the_store_starts_first_and_core_tables_exist_before_any_other_plugin_starts(tmp_path: Path, clock: FakeClock) -> None:
    order: list[str] = []

    class Late(stub("enricher")):  # type: ignore[misc]
        async def start(self, ctx: Any) -> None:
            await super().start(ctx)
            assert ctx.db is not None and await ctx.db.fetchone("SELECT 1 FROM labels") is None  # table is there
            order.append("late")

    host = make(tmp_path, clock, [("enricher", "late", Late), STORE])
    await host.start_store()
    order.append("store-open")
    await host.start()
    await host.stop()
    assert order == ["store-open", "late"]


async def test_no_store_means_the_host_will_not_start(tmp_path: Path, clock: FakeClock) -> None:
    host = make(tmp_path, clock, [("enricher", "e", stub("enricher"))])
    with pytest.raises(PluginError, match="no store"):
        await host.start()


async def test_two_named_instances_of_one_plugin_run_side_by_side(tmp_path: Path, clock: FakeClock) -> None:
    class Backend(stub("llm_backend")):  # type: ignore[misc]
        async def health(self) -> Health:
            return Health(True, self.ctx.config.get("url", ""))

    tables = {"llm_backend": {"desk": {"plugin": "ollama", "url": "http://desk:11434"},
                              "laptop": {"plugin": "ollama", "url": "http://127.0.0.1:11434"}}}  # fmt: skip
    host = make(tmp_path, clock, [STORE, ("llm_backend", "ollama", Backend)], tables)
    await host.start()
    try:
        usable = host.usable("llm_backend")
        assert set(usable) == {"ollama", "desk", "laptop"}
        assert usable["desk"].ctx.config == {"url": "http://desk:11434"}  # the host's keys are not the plugin's
        assert usable["desk"] is not usable["laptop"]
        added = "lab"
        await host.add_instance("llm_backend", added, "ollama", {"url": "http://lab:11434"})
        assert host.usable("llm_backend")[added].ctx.config["url"] == "http://lab:11434"
    finally:
        await host.stop()


async def test_an_instance_of_a_plugin_that_is_not_installed_is_off_not_fatal(tmp_path: Path, clock: FakeClock) -> None:
    host = make(tmp_path, clock, [STORE], {"llm_backend": {"x": {"plugin": "ghost"}}})
    await host.start()
    try:
        (x,) = [s for s in host.statuses() if s.name == "x"]
        assert not x.started and "no llm_backend plugin named 'ghost'" in x.detail
    finally:
        await host.stop()


async def test_a_product_can_register_its_own_kind(tmp_path: Path, clock: FakeClock) -> None:
    kinds = KindRegistry.shipped()
    kinds.register(KindSpec("renderer", "mything.renderers", ("render",)))
    with pytest.raises(Exception, match="already registered"):
        kinds.register(KindSpec("renderer", "other.group"))
    with pytest.raises(Exception, match="already used"):
        kinds.register(KindSpec("renderer2", "mything.renderers"))

    class Render(BasePlugin):
        async def render(self, x: Any) -> str:
            return "ok"

    host = make(tmp_path, clock, [STORE, ("renderer", "html", Render)], kinds=kinds)
    await host.start()
    try:
        assert "html" in host.usable("renderer")
    finally:
        await host.stop()


async def test_a_plugin_missing_a_required_member_is_refused_with_the_reason(tmp_path: Path, clock: FakeClock) -> None:
    class Broken(BasePlugin):
        version = "1"  # no triage(), needs_llm or rank

    host = make(tmp_path, clock, [STORE, ("triager", "broken", Broken), ("enricher", "fine", stub("enricher"))])
    await host.start()
    try:
        by = {s.name: s for s in host.statuses()}
        assert not by["broken"].started and "missing needs_llm" in by["broken"].detail
        assert by["fine"].healthy  # one bad plugin does not stop the rest
    finally:
        await host.stop()


async def test_requires_core_outside_the_range_is_refused(tmp_path: Path, clock: FakeClock) -> None:
    host = make(tmp_path, clock, [STORE, ("enricher", "old", stub("enricher", requires_core="<0.0.0")),
                                  ("enricher", "ok", stub("enricher", requires_core=">=0.0.0"))])  # fmt: skip
    await host.start()
    try:
        by = {s.name: s for s in host.statuses()}
        assert not by["old"].started and "needs oaken-core <0.0.0" in by["old"].detail
        assert by["ok"].healthy
    finally:
        await host.stop()


def analyzer(*needs: str) -> type:
    return stub("enricher", needs=needs)


async def test_a_needs_cycle_switches_off_the_plugin_that_closes_it(tmp_path: Path, clock: FakeClock) -> None:
    host = make(tmp_path, clock, [STORE,
        ("enricher", "a", analyzer("b@self")), ("enricher", "b", analyzer("a@self")),  # b closes the cycle
        ("enricher", "sentiment", analyzer("summary@parent", "sentiment@parent")),      # a parent loop is fine
        ("enricher", "summary", analyzer()),
        ("enricher", "loner", analyzer("loner@self")),                                   # needs itself on one record
        ("enricher", "odd", analyzer("x@cousin")),                                       # unknown relation
    ])  # fmt: skip
    await host.start()
    try:
        by = {s.name: s for s in host.statuses()}
        assert by["a"].healthy and not by["b"].started and "closes a cycle" in by["b"].detail
        assert by["sentiment"].healthy and by["summary"].healthy
        assert "closes a cycle" in by["loner"].detail and "unknown relation" in by["odd"].detail
    finally:
        await host.stop()


class Poker(BasePlugin):
    """Writes labels on request."""

    def labels(self) -> list[BaseLabel]:
        return [BaseLabel.new("heat", "How upset", ("calm", "hot"))]

    async def poke(self, key: str, value: str) -> bool:
        assert self.ctx.labels is not None
        return await self.ctx.labels.set("s1", key, value)


async def with_labels(host: PluginHost, clock: FakeClock) -> LabelStore:
    db = await host.start_store()
    maker = LabelMaker.core()
    store = LabelStore(db, maker, ChangeLog(clock), clock)
    host.attach_labels(store, maker)
    await host.start()
    return store


async def test_a_plugin_writes_labels_as_itself(tmp_path: Path, clock: FakeClock) -> None:
    host = make(tmp_path, clock, [STORE, ("provider", "poker", Poker)])
    store = await with_labels(host, clock)
    try:
        assert await host.usable("provider")["poker"].poke("heat", "hot") is True
        assert await store.get("s1", "heat") == "hot"
        (row,) = await store.log.rows(store.db, "s1")
        assert (row.actor_kind, row.actor, row.layer) == ("system", "provider.poker", "machine")
    finally:
        await host.stop()


async def test_a_bad_label_write_switches_the_plugin_off_and_core_carries_on(tmp_path: Path, clock: FakeClock) -> None:
    bus = EventBus()
    seen = []

    async def on(e: PluginHealthChanged) -> None:  # type: ignore[type-arg]
        seen.append((e.plugin, e.healthy))

    bus.subscribe(PluginHealthChanged, on)
    host = make(tmp_path, clock, [STORE, ("provider", "poker", Poker), ("enricher", "other", stub("enricher"))], bus=bus)
    await with_labels(host, clock)
    try:
        poker = host.usable("provider")["poker"]
        with pytest.raises(LabelError):
            await poker.poke("not-registered", "x")
        by = {s.name: s for s in host.statuses()}
        assert not by["poker"].started and "switched off" in by["poker"].detail
        assert by["other"].healthy
        assert ("provider:poker", False) in seen
        assert "poker" not in host.usable("provider")
        await host.check_health()  # a fault is not retried on its own
        assert not {s.name: s for s in host.statuses()}["poker"].started
    finally:
        await host.stop()


async def test_a_plugin_defining_a_reserved_label_never_starts(tmp_path: Path, clock: FakeClock) -> None:
    class Greedy(BasePlugin):
        def labels(self) -> list[BaseLabel]:
            return [BaseLabel.new("queue")]

    host = make(tmp_path, clock, [STORE, ("provider", "greedy", Greedy)])
    await with_labels(host, clock)
    try:
        (s,) = [s for s in host.statuses() if s.name == "greedy"]
        assert not s.started and "reserved" in s.detail
    finally:
        await host.stop()


async def test_a_plugin_owns_tables_and_they_migrate_once_in_their_own_history(tmp_path: Path, clock: FakeClock) -> None:
    mig = tmp_path / "mig"
    mig.mkdir()
    (mig / "0001_notes.sql").write_text("CREATE TABLE notes_item (id TEXT PRIMARY KEY, body TEXT);")

    class Notes(BasePlugin):
        migrations_dir: ClassVar[Path | None] = mig

    tables = {"provider": {"n1": {"plugin": "notes"}, "n2": {"plugin": "notes"}}}  # two instances, one set of tables
    host = make(tmp_path, clock, [STORE, ("provider", "notes", Notes)], tables)
    db = await host.start_store()
    await host.start()
    try:
        assert await db.fetchone("SELECT 1 FROM notes_item") is None
        rows = await db.fetchall("SELECT version, name FROM schema_version_provider_notes")
        assert [(r["version"], r["name"]) for r in rows] == [(1, "0001_notes")]
        core = await db.fetchall("SELECT name FROM schema_version")
        assert {r["name"] for r in core} == {"0001_core", "0002_jobs", "0003_agents", "0004_exposure", "0005_router", "0006_budget"}  # core's history is its own
        assert all(s.healthy for s in host.statuses() if s.kind == "provider")
    finally:
        await host.stop()


class HiIn(BaseModel):
    who: str = "you"


class HiOut(BaseModel):
    text: str


class Greeter(BasePlugin):
    @command("hi", "Say hi.", input=HiIn, output=HiOut)
    async def hi(self, ctx: PluginOpContext, inp: HiIn) -> HiOut:
        return HiOut(text=f"hi {inp.who} from {ctx.plugin} via {ctx.client}")


async def test_a_command_becomes_an_operation_when_the_plugin_loads_and_goes_when_it_does_not(tmp_path: Path, clock: FakeClock) -> None:
    from oaken_core.operations.caller import CallerContext
    from oaken_core.operations.context import OpContext
    from oaken_core.plugins.ops import plugin_operations

    class Fails(BasePlugin):
        requires_config = ("token",)  # never configured

        @command("nope", "Never offered.", input=HiIn, output=HiOut)
        async def nope(self, ctx: PluginOpContext, inp: HiIn) -> HiOut: ...

    host = make(tmp_path, clock, [STORE, ("provider", "greeter", Greeter), ("provider", "fails", Fails)])
    await host.start()
    try:
        ops = plugin_operations(host)
        assert [o.name for o in ops] == ["greeter.hi"]  # the CLI is built from these, so only this verb exists
        op = ops[0]
        assert (op.permission, op.mcp, op.plugin) == ("read", "off", "greeter")  # off over MCP until the owner says
        out = await op.handler(OpContext(svc=None, caller=CallerContext("cli", USER)), HiIn(who="Drew"))
        assert out.text == "hi Drew from greeter via cli"
    finally:
        await host.stop()


async def test_job_handlers_and_scheduled_tasks_are_registered_from_plugins(tmp_path: Path, clock: FakeClock, kit: Any) -> None:
    from datetime import timedelta

    from oaken_core.plugins.contracts import PluginJobContext, PluginJobHandler, PluginScheduledTask
    from oaken_core.plugins.jobs import register_plugin_jobs, seed_plugin_schedules
    from oaken_core.scheduler import Scheduler

    class Ping(BaseModel):
        n: int = 0

    class Worker(BasePlugin):
        def job_handlers(self) -> list[PluginJobHandler]:
            async def run(ctx: PluginJobContext, p: Ping) -> None:
                return None

            return [PluginJobHandler("ping", Ping, run)]

        def scheduled_tasks(self) -> list[PluginScheduledTask]:
            return [PluginScheduledTask("tick", "plugin.worker.ping", Ping(), every=60)]

    host = make(tmp_path, clock, [STORE, ("provider", "worker", Worker)])
    await host.start()
    try:
        assert register_plugin_jobs(host, kit.runner) == ["plugin.worker.ping"]
        sched = Scheduler(kit.db, kit.queue, clock)
        assert await seed_plugin_schedules(host, sched) == ["plugin.worker.tick"]
        assert await seed_plugin_schedules(host, sched) == []  # only if absent: the owner's edits stand
        assert await sched.next_run("plugin.worker.tick") is not None and timedelta(seconds=60)
    finally:
        await host.stop()


async def test_disable_enable_and_reload_one_plugin_while_core_runs(tmp_path: Path, clock: FakeClock) -> None:
    host = make(tmp_path, clock, [STORE, ("enricher", "e", stub("enricher")), ("enricher", "f", stub("enricher"))])
    await host.start()
    try:
        assert await host.disable("e") == ["e"]
        assert set(host.usable("enricher")) == {"f"}
        await host.enable("e")
        assert set(host.usable("enricher")) == {"e", "f"}
        await host.reload("f")
        assert set(host.usable("enricher")) == {"e", "f"}
        with pytest.raises(NotFound):
            await host.disable("nope")
    finally:
        await host.stop()


async def test_a_plugin_that_fails_to_start_is_retried_at_the_health_tick(tmp_path: Path, clock: FakeClock) -> None:
    state = {"up": False}

    class Flaky(stub("enricher")):  # type: ignore[misc]
        config_model = None

        async def start(self, ctx: Any) -> None:
            if not state["up"]:
                raise RuntimeError("not yet")

    host = make(tmp_path, clock, [STORE, ("enricher", "flaky", Flaky)], {"enricher": {"flaky": {"x": 1}}})
    await host.start()
    try:
        assert "start() failed: not yet" in {s.name: s for s in host.statuses()}["flaky"].detail
        state["up"] = True
        await host.check_health()
        assert {s.name: s for s in host.statuses()}["flaky"].healthy
    finally:
        await host.stop()


async def test_check_config_and_probe_try_a_setting_without_touching_the_live_plugin(tmp_path: Path, clock: FakeClock) -> None:
    class Cfg(BaseModel):
        url: str

    class Remote(stub("llm_backend")):  # type: ignore[misc]
        config_model = Cfg

        async def health(self) -> Health:
            return Health(self.config.url.startswith("http"), "url checked")

    host = make(tmp_path, clock, [STORE, ("llm_backend", "remote", Remote)], {"llm_backend": {"remote": {"url": "http://x"}}})
    await host.start()
    try:
        assert host.check_config("remote", {"url": "http://y"}) == []
        assert "url" in host.check_config("remote", {})[0]
        good, bad = await host.probe("remote", {"url": "http://y"}), await host.probe("remote", {"url": "ftp://y"})
        assert (good[0].ok, bad[0].ok) == (True, False)
        assert host.usable("llm_backend")["remote"].ctx.config["url"] == "http://x"  # the live one is untouched
    finally:
        await host.stop()


async def test_needs_vec_marks_core_only_work_and_is_never_offered_to_an_agent(tmp_path: Path, clock: FakeClock) -> None:
    host = make(tmp_path, clock, [STORE, ("enricher", "dedupe", stub("enricher", needs_llm=True, needs_vec=True)),
                                  ("enricher", "summary", stub("enricher", needs_llm=True)),
                                  ("enricher", "bad", stub("enricher", needs_vec="yes"))])  # fmt: skip
    await host.start()
    try:
        assert host.agent_eligible_kinds() == ["enricher.summary"]  # an agent cannot reach the index
        assert host.needs_vec_kinds() == ["enricher.dedupe"]
        assert "needs_vec must be True or False" in {s.name: s for s in host.statuses()}["bad"].detail
    finally:
        await host.stop()


async def test_agent_eligible_kinds_lists_analyzers_that_need_a_model(tmp_path: Path, clock: FakeClock) -> None:
    host = make(tmp_path, clock, [STORE, ("enricher", "summary", stub("enricher", needs_llm=True)),
                                  ("enricher", "gravatar", stub("enricher", needs_llm=False))])  # fmt: skip
    await host.start()
    try:
        assert host.agent_eligible_kinds() == ["enricher.summary"]
    finally:
        await host.stop()


async def test_a_plain_install_finds_the_sqlite_store_through_real_entry_points(tmp_path: Path, clock: FakeClock) -> None:
    from oaken_core.plugins.host import discover_entry_points

    cfg = PluginTables(tables={"store": {"sqlite": {"path": str(tmp_path / "real.db")}}})
    host = PluginHost(cfg, clock, discover=discover_entry_points)  # no fakes: the installed metadata
    db = await host.start()  # type: ignore[func-returns-value]
    try:
        assert db is None and host.db is not None
        assert (tmp_path / "real.db").exists()
        assert [(s.kind, s.name, s.started) for s in host.statuses() if s.kind == "store"] == [("store", "sqlite", True)]
    finally:
        await host.stop()


async def test_an_llm_analyzer_is_a_kind_base_plus_base_llm_analyzer_in_either_order() -> None:
    class A(BaseLLMAnalyzer, BaseTriager):
        route = {"backend": "desk"}

    class B(BaseTriager, BaseLLMAnalyzer):
        pass

    assert A.needs_llm and B.needs_llm and not BaseTriager.needs_llm
    assert A.rank == 50 and A.prompt == "" and A.guard == ""
    assert not hasattr(BaseTriager, "ask")  # a deterministic analyzer has no model door
    plain = A()
    with pytest.raises(AttributeError):  # ctx is set by start()
        await plain.ask(None, BaseModel, content_hash="h")
