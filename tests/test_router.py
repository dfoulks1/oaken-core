# SPDX-License-Identifier: Apache-2.0
import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import BaseModel

from oaken_core.actors import USER
from oaken_core.clock import FakeClock
from oaken_core.db import Database
from oaken_core.errors import AIBackendError, ConfidentialRefused, ConfigError, Unavailable
from oaken_core.ids import SubjectId
from oaken_core.intake import KIND, Intake, IntakeItem, Verdict
from oaken_core.operations.caller import CallerContext
from oaken_core.plugins.base import BaseEnricher
from oaken_core.plugins.contracts import LLMRequest, PluginContext
from oaken_core.router import Call, Entry, Route, Router
from oaken_core.router.nearjunk import NearJunk
from oaken_core.router.stub import MemoryVectors, StubBackend, StubEmbedder
from oaken_core.settings import RouterSettings
from oaken_core.untrusted import UntrustedText

from .conftest import Kit, Ops

SECRET = "the-merger-closes-friday"
TEXT = "please wire the money to the new account before friday noon to avoid the late fee today"


class Answer(BaseModel):
    label: str
    confidence: float = 1.0


class Dir:
    """The directory a host would give the router: backends by name, an embedder, a vector store."""

    def __init__(self) -> None:
        self.entries: dict[str, Entry] = {}
        self.emb: Entry | None = None
        self.store: MemoryVectors | None = None

    def backends(self) -> Mapping[str, Entry]:
        return self.entries

    def embedder(self) -> Entry | None:
        return self.emb

    def vectors(self) -> MemoryVectors | None:
        return self.store

    def add(self, name: str, plugin: Any, locality: str = "local", concurrency: int = 1) -> Any:
        self.entries[name] = Entry(name, plugin, locality, concurrency)  # type: ignore[arg-type]
        return plugin

    def vec(self, locality: str = "local") -> StubEmbedder:
        emb = StubEmbedder()
        self.emb, self.store = Entry("emb", emb, locality), MemoryVectors()  # type: ignore[arg-type]
        return emb


def answer(label: str = "ok", confidence: float = 1.0) -> dict[str, Any]:
    return {"label": label, "confidence": confidence}


def rig(db: Database, clock: FakeClock, **settings: Any) -> tuple[Router, Dir]:
    d = Dir()
    return Router(db, clock, RouterSettings(**settings), d), d


def call(h: str = "h1", *, confidential: bool = False, needs_vec: bool = False, text: str = "") -> Call:
    return Call("triage", "1", h, confidential, needs_vec, text)


def req(text: str = "hello") -> LLMRequest:
    return LLMRequest(system="rate it", user=(UntrustedText(text, "body"),))


async def test_two_backends_serve_one_model_the_named_one_answers_and_fallback_covers_it_being_down(core_db: Database, clock: FakeClock) -> None:
    router, d = rig(core_db, clock)
    desk, laptop = StubBackend(answer("desk")), StubBackend(answer("laptop"))
    d.add("desk", desk), d.add("laptop", laptop)
    route = Route("desk", "qwen", fallback=("laptop",))
    assert (await router.ask(route, call("a"), req(), Answer)).label == "desk"
    assert [m for _, m in desk.calls] == ["qwen"] and laptop.calls == []
    d.entries["desk"] = Entry("desk", None, "local")  # unhealthy: the host lists it without a plugin
    assert (await router.ask(route, call("b"), req(), Answer)).label == "laptop"
    d.add("desk", desk)
    desk.up = False  # healthy on paper, errors on the call
    assert (await router.ask(route, call("c"), req(), Answer)).label == "laptop"
    laptop.up = False
    with pytest.raises(AIBackendError):
        await router.ask(route, call("d"), req(), Answer)


async def test_a_backend_that_is_down_with_no_fallback_is_unavailable(core_db: Database, clock: FakeClock) -> None:
    router, d = rig(core_db, clock)
    d.entries["desk"] = Entry("desk", None, "local")
    with pytest.raises(Unavailable):
        await router.ask(Route("desk", "m"), call(), req(), Answer)
    with pytest.raises(Unavailable):
        await router.ask(Route("nope", "m"), call(), req(), Answer)


async def test_a_confidential_call_never_reaches_a_lan_or_hosted_backend(core_db: Database, clock: FakeClock) -> None:
    router, d = rig(core_db, clock)
    hosted, lan = StubBackend(answer()), StubBackend(answer())
    d.add("cloud", hosted, "hosted"), d.add("box", lan, "lan")
    for route in (Route("cloud", "m"), Route("box", "m"), Route("cloud", "m", fallback=("box",))):
        with pytest.raises(ConfidentialRefused):
            await router.ask(route, call(confidential=True), req(SECRET), Answer)
    assert hosted.calls == [] and lan.calls == []
    router.settings.lan_allowlist.append("box")  # the owner allowlists the LAN box
    assert (await router.ask(Route("cloud", "m", fallback=("box",)), call(confidential=True), req(), Answer)).label == "ok"
    assert hosted.calls == [] and len(lan.calls) == 1
    d.add("here", StubBackend(answer("here")))
    assert (await router.ask(Route("cloud", "m", fallback=("here",)), call("x", confidential=True), req(), Answer)).label == "here"
    assert (await router.ask(Route("cloud", "m"), call("y"), req(), Answer)).label == "ok"  # not confidential: fine


async def test_changing_the_model_changes_the_cache_key(core_db: Database, clock: FakeClock) -> None:
    router, d = rig(core_db, clock)
    be = d.add("desk", StubBackend(answer()))
    await router.ask(Route("desk", "small"), call(), req(), Answer)
    await router.ask(Route("desk", "small"), call(), req(), Answer)  # same key: no second call
    assert len(be.calls) == 1
    await router.ask(Route("desk", "large"), call(), req(), Answer)
    assert [m for _, m in be.calls] == ["small", "large"]
    await router.ask(Route("desk", "small"), Call("triage", "2", "h1"), req(), Answer)  # a new version is a new key
    assert len(be.calls) == 3
    (row,) = [r for r in await router.usage() if r.model == "small"]
    assert (row.calls, row.cache_hits) == (2, 1)


async def test_an_unset_model_is_the_backends_default_and_keys_on_it(core_db: Database, clock: FakeClock) -> None:
    router, d = rig(core_db, clock)
    be = d.add("desk", StubBackend(answer(), model_id="house"))
    await router.ask(Route("desk"), call(), req(), Answer)
    await router.ask(Route("desk"), call(), req(), Answer)
    assert len(be.calls) == 1 and be.calls[0][1] == "house"
    be.up = False
    assert (await router.ask(Route("desk"), call(), req(), Answer)).label == "ok"  # cached: no backend needed


async def test_a_near_duplicate_is_answered_from_its_neighbour_with_no_model_call(core_db: Database, clock: FakeClock) -> None:
    router, d = rig(core_db, clock, near_duplicate_score=0.9)
    be, emb = d.add("desk", StubBackend(answer("scam"))), d.vec()
    route = Route("desk", "m")
    first = await router.ask(route, call("h1", needs_vec=True, text=TEXT), req(TEXT), Answer)
    near = await router.ask(route, call("h2", needs_vec=True, text=TEXT + " thanks"), req(TEXT), Answer)
    assert first == near and len(be.calls) == 1  # one model call for two items
    far = await router.ask(route, call("h3", needs_vec=True, text="quarterly report attached see page two"), req(), Answer)
    assert far.label == "scam" and len(be.calls) == 2
    (row,) = await router.usage()
    assert (row.calls, row.shortcut_hits) == (2, 1) and row.tokens_saved == 110
    router.settings.shortcuts = False  # the switch turns the whole rung off
    await router.ask(route, call("h4", needs_vec=True, text=TEXT + " again"), req(), Answer)
    assert len(be.calls) == 3


async def test_a_needs_vec_call_waits_when_the_embedder_is_down_and_refuses_a_hosted_one_for_confidential(core_db: Database, clock: FakeClock) -> None:
    router, d = rig(core_db, clock)
    d.add("desk", StubBackend(answer()))
    with pytest.raises(Unavailable):
        await router.ask(Route("desk", "m"), call(needs_vec=True, text=TEXT), req(), Answer)
    assert not router.vec_ready()
    d.vec("hosted")
    assert router.vec_ready()
    with pytest.raises(ConfidentialRefused):
        await router.ask(Route("desk", "m"), call(needs_vec=True, text=TEXT, confidential=True), req(), Answer)
    assert (await router.ask(Route("desk", "m"), call(needs_vec=True, text=TEXT), req(), Answer)).label == "ok"


async def test_a_low_confidence_answer_escalates_once_and_the_big_answer_is_cached(core_db: Database, clock: FakeClock) -> None:
    router, d = rig(core_db, clock)
    small, big = d.add("small", StubBackend(answer("meh", 0.2))), d.add("big", StubBackend(answer("sure", 0.9)))
    route = Route("small", "s", escalate_backend="big", escalate_model="b")
    assert (await router.ask(route, call(), req(), Answer)).label == "sure"
    assert len(small.calls) == 1 and len(big.calls) == 1
    assert (await router.ask(route, call(), req(), Answer)).label == "sure"  # found under the big model's key
    assert len(small.calls) == 1 and len(big.calls) == 1
    rows = {r.model: r for r in await router.usage()}
    assert rows["s"].escalations == 1 and rows["b"].calls == 1 and rows["b"].cache_hits == 0 and rows["s"].cache_hits == 1


async def test_escalation_happens_once_even_if_the_big_answer_is_also_weak(core_db: Database, clock: FakeClock) -> None:
    router, d = rig(core_db, clock)
    small, big = d.add("small", StubBackend(answer("a", 0.1))), d.add("big", StubBackend(answer("b", 0.1)))
    assert (await router.ask(Route("small", "s", escalate_backend="big", escalate_model="b"), call(), req(), Answer)).label == "b"
    assert (len(small.calls), len(big.calls)) == (1, 1)


async def test_low_confidence_with_nowhere_to_escalate_keeps_the_answer(core_db: Database, clock: FakeClock) -> None:
    router, d = rig(core_db, clock)
    be = d.add("small", StubBackend(answer("meh", 0.2)))
    assert (await router.ask(Route("small", "s"), call(), req(), Answer)).label == "meh"
    big = Route("small", "s", escalate_backend="far")  # the big box is not there
    assert (await router.ask(big, call("h2"), req(), Answer)).label == "meh" and len(be.calls) == 2


async def test_a_confidential_call_does_not_escalate_to_a_hosted_model(core_db: Database, clock: FakeClock) -> None:
    router, d = rig(core_db, clock)
    d.add("small", StubBackend(answer("meh", 0.2)))
    cloud = d.add("cloud", StubBackend(answer("sure")), "hosted")
    route = Route("small", "s", escalate_backend="cloud", escalate_model="b")
    assert (await router.ask(route, call(confidential=True), req(), Answer)).label == "meh"
    assert cloud.calls == []


async def test_a_bad_answer_fails_and_stores_nothing_unless_the_escalation_fixes_it(core_db: Database, clock: FakeClock) -> None:
    router, d = rig(core_db, clock)
    d.add("small", StubBackend({"wrong": 1}))
    with pytest.raises(AIBackendError):
        await router.ask(Route("small", "s"), call(), req(), Answer)
    d.add("big", StubBackend({"still": "wrong"}))
    with pytest.raises(AIBackendError):
        await router.ask(Route("small", "s", escalate_backend="big", escalate_model="b"), call(), req(), Answer)
    assert await core_db.fetchone("SELECT 1 FROM answer_cache") is None  # nothing stored
    d.add("big", StubBackend(answer("fixed")))
    assert (await router.ask(Route("small", "s", escalate_backend="big", escalate_model="b"), call(), req(), Answer)).label == "fixed"
    text = StubBackend()
    text.reply = {}  # a non-JSON text answer is also a bad answer
    d.add("text", text)

    async def gen(request: LLMRequest, *, model: str | None = None) -> Any:
        from oaken_core.plugins.contracts import LLMResponse

        return LLMResponse(text="not json")

    text.generate = gen  # type: ignore[method-assign]
    with pytest.raises(AIBackendError, match="not valid JSON"):
        await router.ask(Route("text", "t"), call("z"), req(), Answer)


async def test_each_backend_has_its_own_semaphore(core_db: Database, clock: FakeClock) -> None:
    router, d = rig(core_db, clock)
    release, running, peak = asyncio.Event(), 0, 0

    class Slow(StubBackend):
        async def generate(self, request: LLMRequest, *, model: str | None = None) -> Any:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await release.wait()
            running -= 1
            return await super().generate(request, model=model)

    d.add("slow", Slow(answer()), concurrency=2), d.add("quick", StubBackend(answer("quick")))
    slow = [asyncio.create_task(router.ask(Route("slow", "m"), call(f"s{i}"), req(), Answer)) for i in range(4)]
    await asyncio.sleep(0.05)
    assert peak == 2  # two at a time, two waiting
    assert (await router.ask(Route("quick", "m"), call(), req(), Answer)).label == "quick"  # not stalled by the slow box
    release.set()
    await asyncio.gather(*slow)
    assert peak == 2


async def test_input_is_cut_to_the_budget_and_the_context_window(core_db: Database, clock: FakeClock) -> None:
    router, d = rig(core_db, clock, default_input_tokens=10, default_output_tokens=7)
    be = d.add("desk", StubBackend(answer(), context_window=100))
    await router.ask(Route("desk", "m"), call(), LLMRequest("sys", (UntrustedText("x" * 500, "body"), "tail")), Answer)
    sent, _ = be.calls[0]
    assert sent.max_output_tokens == 7
    assert [len(p) if isinstance(p, str) else len(p.unwrap()) for p in sent.user] == [40, 0]  # 10 tokens of 4 chars
    small = d.add("tiny", StubBackend(answer(), context_window=20))
    await router.ask(Route("tiny", "m"), call("h2"), LLMRequest("s" * 40, (UntrustedText("y" * 500),)), Answer)
    assert len(small.calls[0][0].user[0].unwrap()) == 3 * 4  # window 20, less 7 out and 10 for the system text


def test_a_route_reads_its_table_over_the_analyzers_defaults_and_rejects_bad_shapes() -> None:
    base = Route.from_config({"backend": "desk", "model": "m", "fallback": ["a"], "escalate": {"backend": "big", "model": "b"}})
    assert (base.fallback, base.escalate_backend, base.escalate_model) == (("a",), "big", "b")
    over = Route.from_config({"model": "other", "fallback": []}, base)
    assert (over.backend, over.model, over.fallback, over.escalate_model) == ("desk", "other", (), "b")
    with pytest.raises(ConfigError):
        Route.from_config({"fallback": "laptop"})
    with pytest.raises(ConfigError):
        Route.from_config({"escalate": "big"})


async def test_an_analyzer_asks_through_ctx_router_with_its_route_and_its_own_name(core_db: Database, clock: FakeClock) -> None:
    router, d = rig(core_db, clock)
    be = d.add("laptop", StubBackend(answer("fine")))

    class Sum(BaseEnricher):
        version = "7"
        needs_llm = True
        route = {"backend": "desk", "model": "m"}  # the default; the owner's table moves it to the laptop

    sum_ = Sum()
    await sum_.start(PluginContext("summary", "enricher", clock, None, router=router, route={"backend": "laptop"}))
    out = await sum_.ask(req(), Answer, content_hash="h")
    assert out.label == "fine" and be.calls[0][1] == "m"
    (row,) = await router.usage("summary")
    assert row.analyzer == "summary" and row.calls == 1
    assert await core_db.fetchone("SELECT 1 FROM answer_cache WHERE analyzer = 'summary' AND version = '7'")
    bare = Sum()
    await bare.start(PluginContext("x", "enricher", clock, None))
    with pytest.raises(Exception, match="no model router"):
        await bare.ask(req(), Answer, content_hash="h")


async def test_the_junk_index_is_off_by_default_and_keeps_only_a_vector_and_a_hash_when_on(core_db: Database, clock: FakeClock) -> None:
    router, d = rig(core_db, clock)
    d.vec()
    assert await router.learn_junk(TEXT) is False and await router.near_junk(TEXT) is None and d.store.items == {}  # type: ignore[union-attr]
    on, d2 = rig(core_db, clock, junk_index=True, nearjunk_score=0.9)
    d2.vec()
    assert await on.learn_junk(TEXT) is True
    ((collection, items),) = d2.store.items.items()  # type: ignore[union-attr]
    assert collection.startswith("junk:") and len(items) == 1 and TEXT not in repr(items)
    assert await on.near_junk(TEXT + " now") is not None
    assert await on.near_junk("lunch on thursday with the design team") is None
    assert await core_db.fetchone("SELECT 1 FROM answer_cache") is None


async def test_a_reject_and_learn_embeds_the_item_and_a_nearjunk_triager_then_rejects_a_close_one_with_no_model_call(kit: Kit) -> None:
    router, d = rig(kit.db, kit.clock, junk_index=True, nearjunk_score=0.9)
    be, emb = d.add("desk", StubBackend(answer())), d.vec()

    class Learn:
        name, rank = "rules", 10

        async def triage(self, item: IntakeItem) -> Verdict | None:
            return Verdict("reject", "spam", learn=True) if item.body == TEXT else None

    nj = NearJunk()
    nj.name = "nearjunk"  # type: ignore[attr-defined]  # intake names its triagers; the host's name goes here
    await nj.start(PluginContext("nearjunk", "triager", kit.clock, None, router=router))
    stored: list[IntakeItem] = []

    async def ingest(item: IntakeItem) -> SubjectId:
        stored.append(item)
        return SubjectId(f"real-{len(stored)}")

    intake = Intake(kit.queue, kit.labels, kit.db, kit.log, triagers=[Learn(), nj], ingest=ingest, router=router)  # type: ignore[list-item]
    intake.register(kit.runner)
    await intake.submit("web", TEXT)
    await kit.runner.drain()
    assert stored == [] and emb.embedded == 1  # rejected, embedded, nothing else
    await intake.submit("web", TEXT + " reply asap")  # not an exact match, so only the vector can catch it
    await intake.submit("web", "lunch on thursday with the design team")
    await kit.runner.drain()
    assert [i.body for i in stored] == ["lunch on thursday with the design team"]
    assert be.calls == []
    rows = [r for r in await kit.log.rows(kit.db) if r.op == "reject"]
    assert len(rows) == 2 and TEXT not in " ".join(r.new_value or "" for r in rows)


async def test_nearjunk_without_a_router_raises_so_intake_backs_up(clock: FakeClock) -> None:
    nj = NearJunk()
    await nj.start(PluginContext("nearjunk", "triager", clock, None))
    with pytest.raises(Unavailable):
        await nj.triage(IntakeItem("s", "web", TEXT, {}))


async def test_the_usage_operation_reports_the_ledger(ops: Ops) -> None:
    router, d = rig(ops.kit.db, ops.kit.clock)
    d.add("desk", StubBackend(answer()))
    ops.svc.router = router
    await router.ask(Route("desk", "m"), call(), req(), Answer)
    await router.ask(Route("desk", "m"), call(), req(), Answer)
    res = await ops.client.call("ai_usage", {}, CallerContext.cli())
    assert res.output is not None
    data = res.output.model_dump()
    assert (data["calls"], data["answered_without_a_model"], data["tokens_spent"], data["tokens_saved"]) == (1, 1, 110, 110)
    assert data["rows"][0]["analyzer"] == "triage"


async def test_the_host_directory_reads_locality_and_concurrency_from_the_instance_tables(tmp_path: Any, clock: FakeClock) -> None:
    from oaken_core.router import HostDirectory

    from .test_host import STORE, make

    tables = {"llm_backend": {"desk": {"plugin": "stub", "locality": "local", "concurrency": 2, "route": {"x": 1}},
                              "cloud": {"plugin": "stub"}, "odd": {"plugin": "stub", "locality": "moon"}}}  # fmt: skip
    host = make(tmp_path, clock, [STORE, ("llm_backend", "stub", StubBackend)], tables)
    await host.start()
    try:
        d = HostDirectory(host, RouterSettings())
        known = d.backends()
        assert known["odd"].locality == "hosted"  # a typo gets the strictest answer
        assert (known["desk"].locality, known["desk"].concurrency, known["desk"].plugin is not None) == ("local", 2, True)
        assert (known["cloud"].locality, known["cloud"].concurrency) == ("hosted", 1)  # unset is the strictest
        assert host._records[("llm_backend", "desk")].ctx.config == {}  # type: ignore[union-attr]  # host keys stay out of the plugin's config
        assert d.embedder() is None and d.vectors() is None
    finally:
        await host.stop()
