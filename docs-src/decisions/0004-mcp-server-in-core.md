# 4. The MCP server is part of core

- Status: accepted
- Date: 2026-10-20

## Context

ADR 0002 described MCP as a thin stdio shim, one client among three. That undersells it. A
local MCP server over sensitive mail is the central offering: it lets a stronger model
analyse and act on mail through a narrow, audited surface while the mail itself stays on the
machine. Any other client is a convenience on top of the same core, and must not be a dependency of
the MCP server, nor the reverse.

## Decision

Core hosts the MCP server. The local API has two surfaces over the same operation registry:

- the **general API** (HTTP and server-sent events), used by the CLI and other local clients;
- the **MCP server**, the AI-enabled surface, mounted at `/mcp` on the same loopback listener
  using MCP's streamable HTTP transport.

The MCP surface is separated from the general one:

- its own token, with a narrower scope than the general API token;
- the registry caller class `mcp` is set by the mount itself, not by a client-declared header,
  so it forces the two-phase confirm flow and can never be raised;
- Host and Origin checks apply, so a web page cannot reach it;
- separate concurrency and result-size limits, so a heavy MCP call cannot starve other clients;
- `[api] mcp = true|false`, and neither surface requires the other to be running.

`oaken mcp` remains as a small stdio bridge to the in-core server for MCP clients that can
only launch stdio subprocesses (Claude Desktop and similar). It adds no behaviour of its own.
Tools are generated from the registry (`mcp_exposed`); sending is never exposed.

## Consequences

- The MCP server's guarantees (untrusted-text envelope, confirm gate, audit log, no send,
  bounded results) are enforced in core and tested there, not in a separate process.
- Prompt 18 now builds the in-core server first and the stdio bridge second; prompt 16 must
  leave a place to mount it.
- ADR 0002 stands for everything else (one daemon, thin clients, two databases, plugins).

## As built (prompt 18)

The server is the official SDK's streamable HTTP, stateless and answering in JSON, so a call holds
no long-lived stream against the concurrency limit. The tool list is the registry as an MCP caller
may see it, and each call goes through `CoreClient.call`. The two-phase confirm, the untrusted
envelope and the result shaping are the only things the transport adds. A swarm agent has a separate
door, `/mcp/agent`, for the job operations (an agent-only registry; its token opens nothing else),
so an agent's identity is the token and the job rules are the HTTP agent routes'. A door is exactly
its own path. The user-facing page is "Connect an AI client" in the docs site.
