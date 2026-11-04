# Step 2: jobs, named queues, intake

Goal: work moves through named queues. A triager labels an item before anything is stored.

Lift and adapt from oaken-mail:
- `core/jobs.py`. `JobQueue` and `JobRunner`. Rename `account_id` to `subject`. Add a `queue` column.
  Drop the mail-only quota and hidden-account code.
- `core/scheduler.py` and `core/cron.py` (cron is done).
- `ingest/pipeline_control.py`. Durable pause and limits. They become per queue.
- `core/operations/builtin/jobs.py`. `retry_job` becomes `queue.requeue`.
- A small generic event bus. Write it new. Mail's `events.py` has about 50 mail events. Take none.

Build:
1. `oaken_core.new_queue(name, description, workers, ...)`. One `default` always exists. It cannot be removed.
2. Routing. A job's queue is its `queue` label, effective value. No label means `default`.
   Jobs never choose a queue. Scheduled and maintenance jobs name theirs directly.
3. Intake is a queue and a stage. Only triagers run there. The job holds a payload and a provisional
   subject id. Nothing is stored yet.
   - Triager labels land on the provisional subject. They move to the real subject at ingest.
   - Reject: drop the payload. Write one content-free change-log row (time, source, verdict, reason, hash).
   - Accept: call an `ingest(payload) -> SubjectId` seam. A domain plugin fills it later.
4. A failing triager leaves intake backed up. Retry with backoff to `max_attempts`, then hold as failed.
   Never auto-purge intake. Config may open the gate on the last failed attempt. The default is off.
5. `queue.requeue`. Takes a job or a selector. `--to <queue>`. `--retriage` clears the machine
   layer and keeps the human layer. Preview and confirm. `mcp="never"`. Config may lift it.
   Rate-limit it per job. A requeue writes a change-log row.
6. Per-queue depth, oldest-item age, last error. Through the operation registry.

Done when:
- A queue created in code receives jobs routed by label.
- A backed-up intake loses nothing and reports its depth and age.
- A requeue by selector replays failed intake jobs.
- A reject stores nothing and logs one row.
- A `queue` label naming a missing queue raises `LabelError`.
