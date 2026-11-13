# SPDX-License-Identifier: Apache-2.0
import json

from oaken_core.actors import USER
from oaken_core.ids import SubjectId
from oaken_core.intake import FORMAT_KIND, KIND, STORE_KIND, Intake, IntakeItem, Verdict
from oaken_core.jobs import Selector
from oaken_core.settings import JobsSettings

from .conftest import Kit
from .test_jobs import Ping

SECRET = "the quick brown fox"


class Rules:
    name, rank = "rules", 10

    def __init__(self, verdict: Verdict | None) -> None:
        self.verdict, self.calls, self.seen = verdict, 0, []

    async def triage(self, item: IntakeItem) -> Verdict | None:
        self.calls += 1
        self.seen.append(item.verdicts)
        return self.verdict


class Model(Rules):
    name, rank = "model", 90


class Broken(Rules):
    name, rank = "broken", 10

    async def triage(self, item: IntakeItem) -> Verdict | None:
        raise RuntimeError("model is down")


def make(kit: Kit, triagers: list, stored: list | None = None, ingest: bool = True) -> Intake:  # type: ignore[type-arg]
    stored = stored if stored is not None else []

    async def do_ingest(item: IntakeItem) -> SubjectId:
        stored.append(item)
        return SubjectId(f"real-{len(stored)}")

    intake = Intake(kit.jobs, kit.labels, kit.db, kit.log, triagers=triagers,
                    ingest=do_ingest if ingest else None)  # fmt: skip
    intake.register(kit.runner)
    return intake


async def test_a_reject_stores_nothing_and_logs_one_content_free_row(kit: Kit) -> None:
    stored: list = []  # type: ignore[type-arg]
    intake = make(kit, [Rules(Verdict("reject", "junk", {"priority": "low"}))], stored)
    prov = await intake.submit("web", {"text": SECRET}, labels={"confidential": "true"})
    await kit.runner.drain()
    assert stored == []
    assert await kit.labels.all(prov) == {}  # the provisional labels are gone too
    (job,) = await kit.jobs.query(kind=KIND)
    assert job.status == "succeeded" and job.payload_json == "{}" and SECRET not in (job.result_json or "")
    rows = [r for r in await kit.log.rows(kit.db, prov) if r.op == "reject"]
    assert len(rows) == 1
    meta = json.loads(rows[0].new_value or "")
    assert meta["reason"] == "junk" and len(meta["sha256"]) == 64
    assert SECRET not in json.dumps([r.__dict__ for r in await kit.log.rows(kit.db)])


async def test_an_accept_ingests_and_the_labels_follow_the_item(kit: Kit) -> None:
    kit.queues.new_queue("enrichers")
    stored: list = []  # type: ignore[type-arg]
    intake = make(kit, [Rules(Verdict("accept", "fit", {"queue": "enrichers", "priority": "high"}))], stored)
    prov = await intake.submit("web", {"text": SECRET}, labels={"confidential": "true"})
    await kit.runner.drain()
    assert [i.body for i in stored] == [{"text": SECRET}]
    assert await kit.labels.all(prov) == {}
    assert await kit.labels.all("real-1") == {"queue": "enrichers", "priority": "high", "confidential": "true", "format": "none"}
    nxt = await kit.jobs.enqueue("later", Ping(), subject="real-1")
    assert (await kit.jobs.get(nxt.job_id)).queue == "enrichers"  # type: ignore[union-attr]
    assert any(r.op == "ingest" for r in await kit.log.rows(kit.db, "real-1"))
    (job,) = await kit.jobs.query(kind=KIND)
    assert job.payload_json == "{}"  # the data lives in the store now


async def _run(kit: Kit, triagers: list, stored: list) -> None:  # type: ignore[type-arg]
    await make(kit, triagers, stored).submit("web", "x")
    await kit.runner.drain()


async def test_any_no_ends_it_and_a_yes_does_not(kit: Kit) -> None:
    rules, model = Rules(Verdict("accept")), Model(Verdict("reject"))
    stored: list = []  # type: ignore[type-arg]
    await _run(kit, [model, rules], stored)
    assert (rules.calls, model.calls, len(stored)) == (1, 1, 0)  # the yes did not stop the model; its no dropped the item


async def test_a_no_ends_the_chain_so_nobody_after_it_is_asked(kit: Kit) -> None:
    first, last = Rules(Verdict("reject")), Model(Verdict("accept"))
    stored: list = []  # type: ignore[type-arg]
    await _run(kit, [first, last], stored)
    assert (first.calls, last.calls, len(stored)) == (1, 0, 0)


async def test_the_last_to_answer_decides_and_sees_what_the_others_said(kit: Kit) -> None:
    rules, model = Rules(Verdict("unsure", "not mine to say", {"priority": "low"})), Model(Verdict("accept", "yes"))
    stored: list = []  # type: ignore[type-arg]
    await _run(kit, [rules, model], stored)
    assert len(stored) == 1 and list(model.seen[0]) == ["rules"]  # the last one saw the first's "I don't know"


async def test_a_pass_goes_to_the_next_triager_and_no_verdict_means_unsure(kit: Kit) -> None:
    rules, model = Rules(None), Model(Verdict("unsure", "ask a human"))
    stored: list = []  # type: ignore[type-arg]
    intake = make(kit, [rules, model], stored)
    await intake.submit("web", "x")
    await kit.runner.drain()
    assert (rules.calls, model.calls, len(stored)) == (1, 1, 1)


async def test_no_verdict_at_all_means_unsure_and_the_item_is_ingested(kit: Kit) -> None:
    stored: list = []  # type: ignore[type-arg]
    intake = make(kit, [Rules(None)], stored)
    await intake.submit("web", "x")
    await kit.runner.drain()
    assert len(stored) == 1


async def test_a_broken_triager_backs_up_intake_and_loses_nothing(kit: Kit) -> None:
    stored: list = []  # type: ignore[type-arg]
    intake = make(kit, [Broken(None)], stored)
    await intake.submit("web", {"text": SECRET})
    for _ in range(4):
        await kit.runner.drain()
        kit.clock.advance(seconds=1000)
    (job,) = await kit.jobs.query(kind=KIND)
    assert job.status == "failed" and SECRET in job.payload_json  # payload kept, never purged
    assert stored == []
    default = (await kit.jobs.stats())[0]
    assert default.failed == 1 and "model is down" in (default.last_error or "")


async def test_requeue_replays_failed_intake_once_the_triager_is_fixed(kit: Kit) -> None:
    stored: list = []  # type: ignore[type-arg]
    triager = Broken(None)
    intake = make(kit, [triager], stored)
    await intake.submit("web", "x")
    for _ in range(4):
        await kit.runner.drain()
        kit.clock.advance(seconds=1000)
    intake.triagers[:] = [Rules(Verdict("accept"))]  # the fix
    res = await kit.jobs.requeue(Selector(status="failed", kind=KIND), USER, retriage=True)
    assert len(res.requeued) == 1
    await kit.runner.drain()
    assert len(stored) == 1


async def test_open_gate_treats_the_last_failure_as_unsure(kit: Kit) -> None:
    kit.jobs.settings = JobsSettings(backoff_jitter=0, backoff_base_seconds=1, open_gate_on_final_failure=True)
    stored: list = []  # type: ignore[type-arg]
    intake = make(kit, [Broken(None)], stored)
    await intake.submit("web", "x")
    for _ in range(4):
        await kit.runner.drain()
        kit.clock.advance(seconds=1000)
    assert len(stored) == 1  # ingested on the final attempt, not stuck


async def test_a_triager_naming_a_missing_queue_fails_for_good(kit: Kit) -> None:
    intake = make(kit, [Rules(Verdict("accept", "", {"queue": "nope"}))])
    await intake.submit("web", "x")
    await kit.runner.drain()
    (job,) = await kit.jobs.query(kind=KIND)
    assert job.status == "failed" and job.attempts == 1 and "no queue named" in (job.last_error or "")


async def test_with_no_ingest_hook_the_item_waits(kit: Kit) -> None:
    intake = make(kit, [Rules(Verdict("accept"))], ingest=False)
    await intake.submit("web", {"text": SECRET})
    await kit.runner.drain()
    (job,) = await kit.jobs.query(kind=STORE_KIND)  # waits in the store stage; intake is not held up
    assert job.status == "queued" and SECRET in job.payload_json


async def test_unsure_can_be_set_to_reject_and_then_nothing_is_stored(kit: Kit) -> None:
    kit.jobs.settings = JobsSettings(store_by_default=False)
    stored: list = []  # type: ignore[type-arg]
    intake = make(kit, [Rules(Verdict("unsure", "hmm"))], stored)
    await intake.submit("web", "x")
    await kit.runner.drain()
    assert stored == []


async def test_store_all_keeps_junk_and_store_none_keeps_nothing(kit: Kit) -> None:
    stored: list = []  # type: ignore[type-arg]
    intake = make(kit, [Rules(Verdict("reject", "junk", {"store": "true"}))], stored)
    await intake.submit("web", "x")
    await kit.runner.drain()
    assert len(stored) == 1
    kit.jobs.settings = JobsSettings(store_by_default=False)
    intake.triagers[:] = [Rules(Verdict("accept"))]
    await intake.submit("web", "y")
    await kit.runner.drain()
    assert len(stored) == 1  # the second was never stored


async def test_a_store_label_overrides_the_rung_for_one_item(kit: Kit) -> None:
    stored: list = []  # type: ignore[type-arg]
    intake = make(kit, [Rules(Verdict("accept"))], stored)
    await intake.submit("web", "x", labels={"store": "false"})  # a scanner source says: not this one
    await intake.submit("web", "y")
    await kit.runner.drain()
    assert [i.body for i in stored] == ["y"]


class Remember:
    def __init__(self) -> None:
        self.calls: list = []  # type: ignore[type-arg]

    async def remember(self, collection: str, key: str, text: str, *, confidential: bool = False) -> None:
        self.calls.append((collection, key, text, confidential))


async def test_a_stored_item_is_embedded_only_when_labelled_embed_or_by_default(kit: Kit) -> None:
    router = Remember()
    stored: list = []  # type: ignore[type-arg]

    async def ingest(item: IntakeItem) -> SubjectId:
        stored.append(item)
        return SubjectId(f"real-{len(stored)}")

    intake = Intake(kit.jobs, kit.labels, kit.db, kit.log, triagers=[Rules(Verdict("accept"))], ingest=ingest, router=router)
    intake.register(kit.runner)
    await intake.submit("web", "plain")  # no label, default off
    await intake.submit("web", "embed me", labels={"embed": "true", "confidential": "true"})
    await kit.runner.drain()
    assert router.calls == [("items", "real-2", "embed me", True)]


class Splitter:
    async def split(self, raw):  # type: ignore[no-untyped-def]
        head, _, body = raw.partition("\n\n")
        return {"headers": {"name": "headers", "content": head}, "body": {"name": "body", "content": body}}


async def test_a_formatter_splits_the_item_into_parts_that_triage_and_the_ingestor_see(kit: Kit) -> None:
    stored: list = []  # type: ignore[type-arg]
    seen: list = []  # type: ignore[type-arg]

    class Peek(Rules):
        async def triage(self, item):  # type: ignore[no-untyped-def]
            seen.append(sorted(item.parts))
            return Verdict("accept")

    async def ingest(item: IntakeItem) -> SubjectId:
        stored.append(item)
        return SubjectId("real-1")

    intake = Intake(kit.jobs, kit.labels, kit.db, kit.log, triagers=[Peek(None)], ingest=ingest,
                    formatter_for=lambda fmt: Splitter() if fmt == "mail" else None)  # fmt: skip
    intake.register(kit.runner)
    await intake.submit("web", "From: a\n\nhello", format="mail")
    await intake.submit("web", "plain")  # no format: one whole body part
    await kit.runner.drain()
    assert sorted(seen) == [["body"], ["body", "headers"]]
    assert {i.body: sorted(i.parts) for i in stored} == {"From: a\n\nhello": ["body", "headers"], "plain": ["body"]}


async def test_a_second_triager_stage_gets_what_the_first_passed_and_a_plugin_can_be_off_in_one(kit: Kit) -> None:
    kit.queues.new_queue("t1", kind="triager")
    kit.queues.new_queue("t2", kind="triager")
    stored: list = []  # type: ignore[type-arg]

    def source(stage: str):  # type: ignore[no-untyped-def]
        return {"t1": [Rules(None)], "t2": [Rules(Verdict("accept"))]}[stage]

    async def ingest(item: IntakeItem) -> SubjectId:
        stored.append(item)
        return SubjectId("real-1")

    intake = Intake(kit.jobs, kit.labels, kit.db, kit.log, triager_source=source, ingest=ingest)
    intake.register(kit.runner)
    await intake.submit("web", "x")
    await kit.runner.drain()
    assert len(stored) == 1  # t1 passed, t2 accepted


async def test_intake_labels_the_item_triage_pending_a_provider_picks_the_entry_stage_and_a_verdict_clears_it(kit: Kit) -> None:
    kit.queues.new_queue("t1", kind="triager")
    kit.queues.new_queue("t2", kind="triager")
    asked: list = []  # type: ignore[type-arg]

    def source(stage: str):  # type: ignore[no-untyped-def]
        asked.append(stage)
        return [Rules(Verdict("accept"))]

    async def ingest(item: IntakeItem) -> SubjectId:
        return SubjectId("real-1")

    intake = Intake(kit.jobs, kit.labels, kit.db, kit.log, triager_source=source, ingest=ingest)
    intake.register(kit.runner)
    prov = await intake.submit("web", "x", labels={"queue": "t2"})
    await kit.runner.drain()
    assert asked == ["t2"]  # not t1: the label chose it
    assert "triage" not in await kit.labels.all("real-1")  # the verdict cleared it
    assert await kit.labels.all(prov) == {}


class Strict:
    async def split(self, raw):  # type: ignore[no-untyped-def]
        raise ValueError("no section")


async def _format_outcomes(kit: Kit, settings: JobsSettings, formatters: dict) -> tuple[list, list]:  # type: ignore[type-arg]
    kit.jobs.settings = settings
    stored: list = []  # type: ignore[type-arg]

    async def ingest(item: IntakeItem) -> SubjectId:
        stored.append(item)
        return SubjectId(f"real-{len(stored)}")

    intake = Intake(kit.jobs, kit.labels, kit.db, kit.log, triagers=[Rules(Verdict("accept"))], ingest=ingest,
                    formatter_for=lambda fmt: formatters.get(fmt))  # fmt: skip
    intake.register(kit.runner)
    await intake.submit("web", "plain")  # no format asked
    await intake.submit("web", "x", format="nobody-has-this")  # nobody handles it
    await intake.submit("web", "y", format="strict")  # a formatter that cannot parse it
    await kit.runner.drain()
    return stored, await kit.jobs.query(kind=FORMAT_KIND, status="failed")


async def test_by_default_an_unformatted_item_goes_on_whole_and_says_what_happened_in_its_format_label(kit: Kit) -> None:
    stored, failed = await _format_outcomes(kit, JobsSettings(), {"strict": Strict()})
    assert len(stored) == 3 and failed == []
    assert sorted([await kit.labels.get(f"real-{n}", "format") for n in (1, 2, 3)]) == ["failed", "none", "unmatched"]


async def test_a_format_that_fails_or_matches_nothing_can_be_held_in_intakes_failed_jobs_and_requeued(kit: Kit) -> None:
    formatters: dict = {"strict": Strict()}  # type: ignore[type-arg]
    stored, failed = await _format_outcomes(kit, JobsSettings(unmatched_format="fail", failed_format="fail"), formatters)
    assert len(stored) == 1  # only the plain one went on
    assert sorted(j.last_error or "" for j in failed) == [
        "PermanentJobError: no formatter for 'nobody-has-this'", "PermanentJobError: strict formatter could not parse it (ValueError)"]
    assert all('"body"' in j.payload_json for j in failed)  # the payload is kept, so it can be run again
    formatters["nobody-has-this"] = Splitter()  # a formatter arrives
    await kit.jobs.requeue(Selector(status="failed", kind=FORMAT_KIND), USER)
    await kit.runner.drain()
    assert len(stored) == 2  # the one nobody handled is formatted and goes on; the unparseable one fails again
    assert len(await kit.jobs.query(kind=FORMAT_KIND, status="failed")) == 1
