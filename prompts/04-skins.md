# Step 4: CLI, HTTP, MCP

Goal: one operation registry. Three doors.

Lift from oaken-mail:
- `apps/cli`: `schema_flags.py`, `commands/operation_cmd.py`, `client.py`, `output.py`, `exits.py`.
  Take no `*_cmd.py` for mail.
- `api/app.py` (804 lines), `api/server.py`. Trim the mail routes.
- `api/mcp_server.py` (322 lines).
- `core/operations/exposure.py`, `core/operations/client.py`. Drop the `readstate` and `ai.caller` imports.

Write new:
- `Services` and `OpContext`. Generic. They hold what core has: store, queue, labels, log, host. No mail.

Build:
1. Every operation reaches CLI and HTTP. MCP and the console only where its mode says.
2. Flags come from the operation's schema. `grove people add expert` is a product's job, not core's.
3. `mcp="never"` operations stay off MCP. A config file may lift one.
4. Port `tests_to_port/test_registry.py` and `test_gate.py` onto the new rig.

5. The CLI is built at plugin load. The command tree comes from the registry as the host loads
   plugins, so a plugin's commands appear without editing core. `grove people add expert` is
   generated from a plugin's type. A plugin that fails to load drops its commands; the CLI starts.
6. MCP authenticates with an agent token (`oaken_core.agents`). Add a per-agent allowlist of
   operations. The `mcp="never"` ceiling stays above it. An agent actor writes the machine layer.
7. Operations to ship: `queue.list`, `queue.pause`, `queue.resume`, `queue.requeue` (`never`),
   `job.list`, `job.cancel`, `job.claim`, `job.heartbeat`, `job.complete`, `job.fail`,
   `agent.create`, `agent.list`, `agent.revoke`, `events.stream`, `labels.get`, `labels.set`,
   `labels.clear`, `log.list`.

Done when:
- A stub operation runs through CLI, HTTP and MCP with one definition.
- A parity test fails if an operation misses a door its mode allows.
- A mutation over MCP needs a proposal and a token.
