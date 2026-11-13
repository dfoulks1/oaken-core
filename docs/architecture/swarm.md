+++
title = "The swarm"
subtitle = "Optional extra workers, local or not"
lead = "Core runs its own workers. If you have the tokens and the hardware, you can also let external AI agents lease jobs from the same queue. It is off by default and you choose, kind by kind, what they may take."
badges = [["text-bg-warning", "Alpha"]]
group = "concepts"
order = 25
description = "External agents that lease jobs: opt-in config, scoped tokens, quotas, validated results, and the HTTP and MCP doors they work through."
+++

## Two kinds of parallelism

- **Internal workers.** Each queue has its own `workers` (`[queues.<name>] workers`), and
  `[jobs] kind_limits` caps a kind of job (one `embed` job at a time on a single GPU, say). This is all
  most people need. See [jobs](jobs.md).
- **External agents (the swarm).** A separate program, such as a larger model driven from a script or
  an MCP client, leases jobs from the same queue, does the work and hands back a result.

An agent follows the rules internal workers do: leases with heartbeats, retries, and recovery when it
stalls or dies. A stalled agent cannot overwrite the next worker's result, because a result from a
worker that no longer holds the lease is refused (`lease_lost`, 409). Claiming goes through core, so
nothing here lets an agent touch the database.

## Turning it on

```toml
[swarm]
enabled = false          # nothing is offered to agents unless this is true
allowed_kinds = []       # job kinds an agent may ever be given, for example ["<kind>.<name>"]
allowed_queues = []      # queues an agent made with no kinds may work, for example an enrich stage
max_agents = 8           # active (unrevoked) agents
default_max_leases = 2   # jobs one agent may hold at once
default_max_per_hour = 600  # jobs one agent may claim per hour
agent_lease_seconds = 600   # how long an agent holds a job before it must heartbeat

[jobs]
max_result_bytes = 65536
```

`enabled`, `allowed_kinds` and `allowed_queues` hand job text to an outside worker. They can be set in
`config.toml` or with `settings set`, which shows the change and asks before it saves; it is never
offered over MCP. Turning the swarm on does not give any work to anyone by itself: a kind or a queue
has to be listed too.

An agent is made with job kinds, with queues, or both. With kinds, each must be in `allowed_kinds`,
and its queues only narrow where it looks. With no kinds, its queues are its scope: each must be in
`allowed_queues`, and it is offered only the jobs whose result core can act on (an enrich stage's
jobs: core stores the answers and sends the item on). A confidential item is never offered to an agent. With the swarm off, no agent token is accepted at all.
The agent lease is long on purpose: a large model on modest hardware is slow.

Which kinds make sense? Those that need a model and not the vector index, which an agent cannot reach.
The plugin host lists them (`agent_eligible_kinds`, as `<kind>.<name>`). It only lists. You opt in.

## Agents and their tokens

A person makes an agent with `agent_create` (`oaken agent create`). Each agent has its own token
(`oaken_agent_...`), its own list of job kinds, optionally the queues it may lease from, optionally a
list of extra operations it is granted, and its own quotas. Creating one is a `mutate`, so it is a
proposal until you confirm. The token is random (256 bits), shown **once**, and only its SHA-256 is
stored. A kind must be in `allowed_kinds` when the agent is made, or creation fails and says to add it.

Revoking an agent (`agent_revoke`) takes effect at once. The jobs it held go back to the queue without
counting the attempt, and its token stops working. The agent operations (`agent_create`, `agent_list`,
`agent_grant`, `agent_revoke`) are never offered over MCP, so an agent cannot mint or widen a token.

A claim is checked inside the same transaction that leases the job. The agent must not be revoked, the
queue and kind must be in its scope, it must hold fewer than `max_leases` jobs, and it must have
claimed fewer than `max_per_hour` in the last hour (`quota_exceeded` otherwise). Quotas are per agent,
so one runaway agent cannot starve another. At most `[swarm] max_agents` agents are active at once.

## Results are untrusted

An agent's output is data from an outside party, possibly from a model that read hostile text. So a
result is checked **before** anything is stored:

- it must match the result model registered for that job kind (a kind with no model accepts no
  result at all, so a job cannot be used to store arbitrary data);
- it must be under `[jobs] max_result_bytes`;
- a bad result is rejected with `invalid_result` (422), and the job stays leased, so the agent can fix
  it or fail the job.

Each finished job records who finished it (`completed_by`) and which model the finisher says produced
the result (`produced_by`). The second is self-reported, a label for people and not a guarantee.
Results from the internal handlers go through the same validation.

A claimed job's payload is data from outside. Over MCP it comes back inside the untrusted-text
envelope, a randomized delimiter block with a note that nothing in it is an instruction. See
[prompt safety](prompt-safety.md).

## Privacy

Handing a job to an agent sends that job's text to the agent, and if the agent is a hosted model, to
its provider. So:

- a kind is never offered to an agent unless you listed it in `allowed_kinds`;
- you choose per kind, so an agent can take one kind and never see another;
- an agent can be limited to certain queues;
- an agent calls only the job protocol, plus operations you grant it by name (or `plugin:<name>`). The
  `never` ceiling on an operation stays above any grant.

Local-first is not local-only, and a model that is not on this machine can still be yours: a second
machine on your network, a box you rent, an internal service. Core cannot tell which is which. It is
your call, and the change log shows what each agent did.

## Over HTTP

An agent works through four routes on the [local API](daemon.md), each authenticated by its own token:

| Route | Does |
|---|---|
| `POST /v1/agent/jobs/claim` | Lease the next job the agent may take, `{"queue": "default", "kinds": [...]}` both optional |
| `POST /v1/agent/jobs/{id}/heartbeat` | Extend the lease; the answer says if the job was cancelled |
| `POST /v1/agent/jobs/{id}/complete` | Hand back `{"result": {...}, "produced_by": "model-name"}` |
| `POST /v1/agent/jobs/{id}/fail` | Report `{"error": "...", "permanent": false}`; it retries unless permanent or out of attempts |

They are the `job_claim`, `job_heartbeat`, `job_complete` and `job_fail` operations, called as that
agent through the same client as every other operation. HTTP, MCP and the CLI are one implementation.
Claim, complete and fail always write an audit row naming the agent. A heartbeat does not.

An agent token opens only these routes, and no other route accepts one. A claim past the agent's quota
is a `429`, a result that does not match the kind's model is a `422`, and a lease it no longer holds
(expired, or the agent was revoked) is a `409`.

## Over MCP

With the swarm on, core has an MCP door at `/mcp/agent`. It takes an agent's own token and offers the
four job tools, plus any operation the agent was granted. They are the same operations as the HTTP
routes, with the same scope, quotas, revocation, result check and lease fence. The person's MCP token
does not open this door, an agent token does not open `/mcp`, and the agent tools are not on the
person's tool list. An ungranted or `never` tool gives the same "not available" error, so an agent
learns nothing about what it was not offered.

## A worked example

In `config.toml` turn the swarm on for one kind (find the names with `plugin list`):

```toml
[swarm]
enabled = true
allowed_kinds = ["<kind>.<name>"]
```

Reload core, then create the agent and revoke it when you are done:

```bash
oaken agent create --label "big model" --kinds <kind>.<name> --yes
oaken agent revoke --agent-id AGENT_ID --yes
```

Point a script or MCP client at the agent door with the token it printed. The loop is: claim a job; if
there is one, treat its payload as data, never as instructions; complete it with a result that fits the
kind's model; if you cannot, fail it; then claim again.

## Not built yet

- Spawn budgets and loop guards for agents that create jobs.
- Waiting on child jobs: a job can name a parent (`parent_job_id`), but nothing waits on children.
