# SPDX-License-Identifier: Apache-2.0
"""The model router. An analyzer asks; the router finds the cheapest way to answer.

The ladder, cheapest first: the analyzer's own rules (not here), the answer cache, an embedding
shortcut (a near-duplicate's stored answer, for analyzers that declare ``needs_vec``), a small
model with a length-capped answer, then one escalation to a larger model when the answer is
low-confidence or fails its schema. Every rung is counted in the ledger.

``LLMRequest`` and ``LLMResponse`` are lifted from oaken-mail (cf3b187, in ``plugins/contracts``);
the retry-once-then-fail shape of ``ai/generate.py`` became escalate-once-then-fail. Dropped:
workspaces and the single global model gate (each backend has its own semaphore).

Confidential calls: a ``local`` backend is always allowed, a ``lan`` one only if the owner lists it
in ``[router] lan_allowlist``, a ``hosted`` one never. A backend that is not allowed is skipped, and
when none is left the router refuses (``ConfidentialRefused``). It never reaches a remote box.
shortcut: backend ``concurrency`` is read when the backend is first used; a change needs a restart.
"""

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from oaken_core.clock import Clock
from oaken_core.db import Database
from oaken_core.errors import (
    AIBackendError,
    ConfidentialRefused,
    ConfigError,
    Deferred,
    Unavailable,
)
from oaken_core.logging import get_logger
from oaken_core.plugins.contracts import Hit, LLMRequest, LLMResponse, VectorItem
from oaken_core.router.store import (
    AnswerCache,
    CacheEntry,
    Ledger,
    LedgerRow,
    Spend,
    SpendRow,
)
from oaken_core.settings import RouterSettings
from oaken_core.untrusted import UntrustedText

log = get_logger("oaken_core.router")
T = TypeVar("T", bound=BaseModel)
Locality = Literal["local", "lan", "hosted"]
LOCALITIES = ("local", "lan", "hosted")
JUNK = "junk"


def content_hash(*parts: str) -> str:
    """A stable hash of the text an answer is about: the usual ``content_hash`` of an analyzer's view."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big") + part.encode())
    return digest.hexdigest()


def estimate(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class Entry:
    """A backend or embedder as the router sees it. ``plugin`` is None when it is not healthy."""

    name: str
    plugin: Any
    locality: Locality = "hosted"  # unset in config means the strictest answer
    concurrency: int = 1


class Directory(Protocol):
    """Where the router finds backends. ``HostDirectory`` reads the plugin host; a test passes a dict."""

    def backends(self) -> Mapping[str, Entry]:
        ...

    def embedder(self) -> Entry | None:
        ...

    def vectors(self) -> Any | None:
        ...


@dataclass(frozen=True)
class Route:
    """What an analyzer asks for. ``model`` empty means the backend's own default. ``escalate`` is
    the larger model tried once on low confidence or a bad answer. Built from the analyzer's
    ``[plugins.<kind>.<name>.route]`` table."""

    backend: str
    model: str = ""
    fallback: tuple[str, ...] = ()
    escalate_backend: str = ""
    escalate_model: str = ""
    input_tokens: int = 0  # 0: the router's default budget
    daily_tokens: int = 0  # 0: no spend budget. Else the most tokens this analyzer may spend a (UTC) day
    min_confidence: float | None = None

    @classmethod
    def from_config(
        cls, cfg: Mapping[str, Any], defaults: "Route | None" = None
    ) -> "Route":
        base = defaults or cls(backend="")
        esc = cfg.get("escalate") or {}
        if not isinstance(esc, Mapping):
            raise ConfigError("route.escalate must be a table with backend and model")
        fallback = cfg.get("fallback", base.fallback)
        if isinstance(fallback, str) or not isinstance(fallback, Sequence):
            raise ConfigError("route.fallback must be a list of backend names")
        return cls(
            backend=str(cfg.get("backend", base.backend)),
            model=str(cfg.get("model", base.model)),
            fallback=tuple(str(f) for f in fallback),
            escalate_backend=str(esc.get("backend", base.escalate_backend)),
            escalate_model=str(esc.get("model", base.escalate_model)),
            input_tokens=int(cfg.get("input_tokens", base.input_tokens)),
            daily_tokens=int(cfg.get("daily_tokens", base.daily_tokens)),
            min_confidence=cfg.get("min_confidence", base.min_confidence),
        )


@dataclass(frozen=True)
class Call:
    """One question. ``content_hash`` identifies what the answer is about (the view supplies it).
    ``text`` is what to embed, and only a ``needs_vec`` analyzer sets it."""

    analyzer: str
    version: str
    content_hash: str
    confidential: bool = False
    needs_vec: bool = False
    text: str = ""


@dataclass(frozen=True)
class Resolved:
    name: str
    entry: Entry
    model: str  # the effective model: the route's, else the backend's default


class HostDirectory:
    """Backends, the embedder and the vector store, from the plugin host. Every table under
    ``[plugins.llm_backend]`` is listed, healthy or not, so locality is known when one is down."""

    def __init__(self, host: Any, settings: RouterSettings) -> None:
        self.host, self.settings = host, settings

    def _entry(self, kind: str, name: str, plugin: Any) -> Entry:
        table = self.host.tables.table(kind, name)
        locality = table.get("locality", "hosted")
        if (
            locality not in LOCALITIES
        ):  # one typo must not take the router down, or open a door
            log.error(
                "unknown locality; treating as hosted",
                kind=kind,
                name=name,
                locality=str(locality),
            )
            locality = "hosted"
        return Entry(name, plugin, locality, max(1, int(table.get("concurrency", 1))))  # type: ignore[arg-type]

    def backends(self) -> Mapping[str, Entry]:
        up = self.host.usable("llm_backend")
        names = set(self.host.tables.tables.get("llm_backend", {})) | set(up)
        return {n: self._entry("llm_backend", n, up.get(n)) for n in sorted(names)}

    def embedder(self) -> Entry | None:
        up = self.host.usable("embedder")
        name = self.settings.embedder or next(iter(up), "")
        return self._entry("embedder", name, up.get(name)) if name else None

    def cache(self, name: str) -> Any | None:
        return self.host.usable("cache_store").get(name)

    def vectors(self) -> Any | None:
        up = self.host.usable("vector_store")
        return (
            up.get(self.settings.vector_store)
            if self.settings.vector_store
            else next(iter(up.values()), None)
        )


class Router:
    def __init__(
        self, db: Database, clock: Clock, settings: RouterSettings, directory: Directory
    ) -> None:
        self.settings, self.directory = settings, directory
        self.builtin_cache = AnswerCache(db, clock)
        self.ledger = Ledger(db, clock)
        self.spend = Spend(db, clock)
        self._gates: dict[str, asyncio.Semaphore] = {}

    # -- who may answer ------------------------------------------------------------------------
    def allowed(self, entry: Entry, confidential: bool) -> bool:
        if not confidential or entry.locality == "local":
            return True
        return entry.locality == "lan" and entry.name in self.settings.lan_allowlist

    def resolve(self, route: Route, call: Call) -> Resolved:
        """The first backend of ``route`` (its own, then its fallbacks in order) that is healthy and,
        for a confidential call, allowed. Raises ``ConfidentialRefused`` if the call's privacy rules out
        every one, ``Unavailable`` if the permitted ones are all down."""
        known = self.directory.backends()
        names = [n for n in (route.backend, *route.fallback) if n]
        if not names:
            raise ConfigError(f"{call.analyzer}: no backend named; set [route] backend")
        permitted = [
            n for n in names if n in known and self.allowed(known[n], call.confidential)
        ]
        if not permitted:
            if call.confidential and any(n in known for n in names):
                raise ConfidentialRefused(
                    f"{call.analyzer}: no local or allowlisted backend for a confidential call"
                )
            raise Unavailable(f"{call.analyzer}: no such backend ({', '.join(names)})")
        for name in permitted:
            entry = known[name]
            if entry.plugin is not None:
                return Resolved(name, entry, route.model or str(entry.plugin.model_id))
        raise Unavailable(f"{call.analyzer}: backend down ({', '.join(permitted)})")

    def _gate(self, entry: Entry) -> asyncio.Semaphore:
        return self._gates.setdefault(entry.name, asyncio.Semaphore(entry.concurrency))

    # -- the ladder ----------------------------------------------------------------------------
    async def ask(
        self, route: Route, call: Call, request: LLMRequest, output: type[T]
    ) -> T:
        """The validated answer to ``request``. A bad answer, after the one escalation, raises
        ``AIBackendError`` and stores nothing."""
        pick = None if route.model else self.resolve(route, call)
        model = route.model or (pick.model if pick else "")
        models = [model, *([route.escalate_model] if route.escalate_model else [])]
        who = pick.name if pick else route.backend

        hit = await self._lookup(call, call.content_hash, models, output)
        if hit is not None:
            value, entry = hit
            await self.ledger.add(
                call.analyzer,
                model,
                who,
                cache_hits=1,
                tokens_saved=entry.tokens_in + entry.tokens_out,
            )
            return value

        vector: list[float] | None = None
        if call.needs_vec and call.text and self.settings.shortcuts:
            vector = (await self.embed([call.text], confidential=call.confidential))[0]
            found = await self._neighbour(call, models, output, vector)
            if found is not None:
                value, entry = found
                await self.ledger.add(
                    call.analyzer,
                    model,
                    who,
                    shortcut_hits=1,
                    tokens_saved=entry.tokens_in + entry.tokens_out,
                )
                return value

        pick = pick or self.resolve(route, call)
        if await self._over(
            route, call
        ):  # the cache and the shortcuts are free; a model call is not
            await self.spend.add(call.analyzer, deferrals=1)
            raise Deferred(
                f"{call.analyzer}: today's token budget is spent",
                self.spend.seconds_to_reset(),
            )
        value = await self._answer(route, call, request, output, pick)
        if vector is not None:
            await self._remember(call, vector)
        return value

    async def _over(self, route: Route, call: Call) -> bool:
        return (
            route.daily_tokens > 0
            and await self.spend.tokens(call.analyzer) >= route.daily_tokens
        )

    async def _answer(
        self,
        route: Route,
        call: Call,
        request: LLMRequest,
        output: type[T],
        pick: Resolved,
    ) -> T:
        """The small model's answer, then at most one escalation. Stores the answer that stands."""
        floor = (
            route.min_confidence
            if route.min_confidence is not None
            else self.settings.min_confidence
        )
        first, problem = await self._call(
            route, call, request, output, pick, fallbacks=True
        )
        confidence = _confidence(first[0]) if first is not None else None
        if first is not None and (confidence is None or confidence >= floor):
            await self._store(call, pick.model, first[1])
            return first[0]
        big = None
        if route.escalate_backend and not await self._over(
            route, call
        ):  # over budget: keep the small answer
            try:
                big = self.resolve(
                    replace(
                        route,
                        backend=route.escalate_backend,
                        fallback=(),
                        model=route.escalate_model,
                    ),
                    call,
                )
            except (
                Unavailable,
                ConfidentialRefused,
            ) as exc:  # nothing bigger may answer: keep what we have
                log.warning(
                    "escalation not possible", analyzer=call.analyzer, reason=exc.code
                )
        if big is None:
            if first is None:
                raise AIBackendError(
                    f"{call.analyzer}: the answer was not valid ({problem})"
                )
            await self._store(
                call, pick.model, first[1]
            )  # low confidence, nothing bigger to ask
            return first[0]
        await self.ledger.add(call.analyzer, pick.model, pick.name, escalations=1)
        second, problem = await self._call(
            route, call, request, output, big, fallbacks=False
        )
        if second is None:
            raise AIBackendError(
                f"{call.analyzer}: the escalated answer was not valid ({problem})"
            )
        await self._store(call, big.model, second[1])
        return second[0]

    async def _call(
        self,
        route: Route,
        call: Call,
        request: LLMRequest,
        output: type[T],
        pick: Resolved,
        *,
        fallbacks: bool,
    ) -> tuple[tuple[T, CacheEntry] | None, str]:
        """One model call, with the route's fallbacks if the backend errors. Returns the validated
        answer (and what to cache), or None and why it was not valid. Counts the tokens either way."""
        candidates = [pick]
        known = self.directory.backends()
        if fallbacks:
            for n in route.fallback:
                e = known.get(n)
                if (
                    n != pick.name
                    and e is not None
                    and e.plugin is not None
                    and self.allowed(e, call.confidential)
                ):
                    candidates.append(
                        Resolved(n, e, route.model or str(e.plugin.model_id))
                    )
        error: Exception | None = None
        for cand in candidates:
            req = self._shape(request, cand)
            try:
                async with self._gate(cand.entry):
                    resp = await asyncio.wait_for(
                        cand.entry.plugin.generate(req, model=cand.model),
                        self.settings.call_timeout_seconds,
                    )
            except Exception as exc:  # noqa: BLE001 - plugin code; the next backend may be up
                log.warning(
                    "backend call failed",
                    analyzer=call.analyzer,
                    backend=cand.name,
                    error=type(exc).__name__,
                )
                error = exc
                continue
            if not isinstance(resp, LLMResponse):
                raise AIBackendError(
                    f"{cand.name}: generate() must return an LLMResponse"
                )
            ran = resp.model_id or cand.model
            tin = resp.input_tokens or estimate(_text(req))
            tout = resp.output_tokens or estimate(resp.text or json.dumps(resp.data))
            await self.ledger.add(
                call.analyzer, ran, cand.name, calls=1, tokens_in=tin, tokens_out=tout
            )
            await self.spend.add(call.analyzer, tin + tout)
            value = _validate(output, resp)
            if isinstance(value, str):
                return None, value
            return (value, CacheEntry(value.model_dump_json(), tin, tout)), ""
        raise AIBackendError(
            f"{call.analyzer}: every backend failed ({type(error).__name__ if error else 'none'})"
        )

    def _shape(self, request: LLMRequest, cand: Resolved) -> LLMRequest:
        """The request as this backend gets it: capped output, input cut to the context window."""
        out = request.max_output_tokens or self.settings.default_output_tokens
        window = int(getattr(cand.entry.plugin, "context_window", 0) or 0)
        budget = self.settings.default_input_tokens
        if window:
            budget = min(budget, max(1, window - out - estimate(request.system)))
        return replace(
            request, max_output_tokens=out, user=_cut(request.user, budget * 4)
        )

    # -- cache and neighbours ------------------------------------------------------------------
    @property
    def cache(self) -> Any:
        """The answer cache: the ``cache_store`` plugin named in ``[router] cache_store`` while it is healthy,
        else core's own."""
        name = self.settings.cache_store
        lookup = getattr(
            self.directory, "cache", None
        )  # a stand-in directory (tests) may not offer one
        plugin = lookup(name) if name and lookup else None
        return plugin if plugin is not None else self.builtin_cache

    async def _lookup(
        self, call: Call, key: str, models: list[str], output: type[T]
    ) -> tuple[T, CacheEntry] | None:
        for model in models:
            cache = self.cache
            entry = await cache.get(cache.key(call.analyzer, call.version, key, model))
            if entry is None:
                continue
            try:
                return output.model_validate_json(entry.result_json), entry
            except ValidationError:
                log.warning(
                    "cached answer no longer fits its schema", analyzer=call.analyzer
                )
        return None

    async def _store(self, call: Call, model: str, entry: CacheEntry) -> None:
        cache = self.cache
        await cache.put(
            cache.key(call.analyzer, call.version, call.content_hash, model), entry
        )

    async def _neighbour(
        self, call: Call, models: list[str], output: type[T], vector: list[float]
    ) -> tuple[T, CacheEntry] | None:
        """The stored answer of a near-duplicate, when one is close enough and still cached."""
        for hit in await self._query(self._answers(call), vector, 2):
            if (
                hit.key != call.content_hash
                and hit.score >= self.settings.near_duplicate_score
            ):
                return await self._lookup(call, hit.key, models, output)
        return None

    def _answers(self, call: Call) -> str:
        return f"answers:{call.analyzer}:{call.version}"

    async def _remember(self, call: Call, vector: list[float]) -> None:
        await self._upsert(self._answers(call), call.content_hash, vector)

    # -- embeddings: the only way an analyzer reaches the vector index --------------------------
    def _embedder(self, confidential: bool) -> Entry:
        entry = self.directory.embedder()
        if entry is None or entry.plugin is None:
            raise Deferred("no healthy embedder", self.settings.wait_seconds)
        if not self.allowed(entry, confidential):
            raise ConfidentialRefused(
                f"embedder {entry.name!r} may not see a confidential item"
            )
        return entry

    def _vectors(self) -> Any:
        store = self.directory.vectors()
        if store is None:
            raise Deferred("no healthy vector store", self.settings.wait_seconds)
        return store

    def vec_ready(self) -> bool:
        e = self.directory.embedder()
        return (
            e is not None
            and e.plugin is not None
            and self.directory.vectors() is not None
        )

    async def embed(
        self, texts: Sequence[str], *, confidential: bool = False
    ) -> list[list[float]]:
        entry = self._embedder(confidential)
        async with self._gate(replace(entry, name=f"embed:{entry.name}")):
            vecs = await asyncio.wait_for(
                entry.plugin.embed(list(texts)), self.settings.call_timeout_seconds
            )
        dims = int(entry.plugin.dimensions)
        if len(vecs) != len(texts) or any(len(v) != dims for v in vecs):
            raise AIBackendError(
                f"embedder {entry.name!r} returned vectors that do not fit {len(texts)} x {dims}"
            )
        return [list(v) for v in vecs]

    def _collection(self, name: str) -> str:
        e = self.directory.embedder()
        return (
            f"{name}:{e.plugin.model_id}" if e and e.plugin else name
        )  # a model's vectors never mix with another's

    async def _query(self, collection: str, vector: list[float], k: int) -> list[Hit]:
        return list(
            await self._vectors().query(self._collection(collection), tuple(vector), k)
        )

    async def _upsert(self, collection: str, key: str, vector: list[float]) -> None:
        await self._vectors().upsert(
            self._collection(collection), [VectorItem(key, tuple(vector))]
        )

    async def nearest(
        self,
        collection: str,
        query: str | Sequence[float],
        k: int = 1,
        *,
        confidential: bool = False,
    ) -> list[Hit]:
        """The ``k`` stored items closest to ``query`` (text is embedded first), best first. For a
        ``needs_vec`` analyzer's nearest-example verdicts. Vectors never go to a model."""
        vec = (
            (await self.embed([query], confidential=confidential))[0]
            if isinstance(query, str)
            else list(query)
        )
        return await self._query(collection, vec, k)

    async def remember(
        self, collection: str, key: str, text: str, *, confidential: bool = False
    ) -> None:
        """Add ``text``'s vector to ``collection`` under ``key`` (a content hash)."""
        await self._upsert(
            collection, key, (await self.embed([text], confidential=confidential))[0]
        )

    # -- junk: reject and learn ----------------------------------------------------------------
    async def learn_junk(self, text: str, *, confidential: bool = False) -> bool:
        """Embed a rejected item and keep its vector and a hash, nothing else. Off unless
        ``[router] junk_index``: a vector can be partly inverted, so the switch is the owner's."""
        if not self.settings.junk_index:
            return False
        await self.remember(JUNK, content_hash(text), text, confidential=confidential)
        return True

    async def near_junk(self, text: str, *, confidential: bool = False) -> Hit | None:
        """A stored junk vector close enough to ``text``, or None. Always None while the index is off."""
        if not self.settings.junk_index:
            return None
        hits = await self.nearest(JUNK, text, 1, confidential=confidential)
        return (
            hits[0] if hits and hits[0].score >= self.settings.nearjunk_score else None
        )

    # -- the ledger ----------------------------------------------------------------------------
    async def usage(self, analyzer: str | None = None) -> list[LedgerRow]:
        return await self.ledger.rows(analyzer)

    async def spent_today(self, analyzer: str | None = None) -> list[SpendRow]:
        return await self.spend.rows(analyzer)


def _text(request: LLMRequest) -> str:
    return request.system + "".join(
        p if isinstance(p, str) else p.unwrap() for p in request.user
    )


def _cut(
    parts: tuple[str | UntrustedText, ...], chars: int
) -> tuple[str | UntrustedText, ...]:
    """The user parts, trimmed in order to ``chars`` characters in all. Later parts go first."""
    out: list[str | UntrustedText] = []
    for part in parts:
        text = part if isinstance(part, str) else part.unwrap()
        keep = text[: max(chars, 0)]
        chars -= len(keep)
        out.append(keep if isinstance(part, str) else replace(part, value=keep))
    return tuple(out)


def _confidence(value: BaseModel) -> float | None:
    c = getattr(value, "confidence", None)
    return float(c) if isinstance(c, int | float) and not isinstance(c, bool) else None


def _validate(output: type[T], resp: LLMResponse) -> T | str:
    """The validated answer, or what was wrong with it (never its content)."""
    data: Any = resp.data
    if data is None:
        try:
            data = json.loads(resp.text)
        except ValueError:
            return "not valid JSON"
    try:
        return output.model_validate(data)
    except ValidationError as exc:
        return "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'value'}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)[:5]
        )
