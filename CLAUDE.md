# agent_runtime

**The execution runtime for declaratively-defined agents** — an "agent farm": one lean async
process hosting many agents as dormant config records, triggered by `agent_bus` events (from
`agent_scheduler`), each run as a transient bounded task that delegates reasoning to `agent_server`,
tools to MCP, retrieval to RAG, and delivery to a channel.

Layering: **patron (authoring) → compiler → runtime DSL → agent_runtime (execute)**. This runtime
consumes **only the runtime DSL** — nothing patron/litegraph-shaped reaches it.

## Status
**Designed, not yet implemented.** The `documents/` are the canonical specs; there is no code/skeleton yet.

## Start here (read in order)
1. [documents/technical_architecture.md](documents/technical_architecture.md) — design, execution model, bus integration.
2. [documents/runtime_dsl_specification.md](documents/runtime_dsl_specification.md) — the runtime DSL contract (the keystone).
3. [documents/implementation_plan.md](documents/implementation_plan.md) — build order (Steps 0–6) **+ open decisions**.
4. [documents/use_cases.md](documents/use_cases.md) — the News Agent worked end-to-end.

## First task
Build the **News Agent vertical slice** per the implementation plan:
`scheduler cron → bus event → farm → newsapi MCP tool → news_curator preset → WhatsApp delivery`.
Hand-write the runtime DSL (do **not** wire patron yet). The slice forces the minimal DSL + executor
into existence. Resolve the plan's open decisions as you go — the big one is **bus client: vendor vs a
shared package** (this would be the 3rd copy of `envelope.py`: agent_bus → agent_scheduler → here).

## Key constraints
- **glibc base only** (`python:3.12-slim-bookworm`) — `valkey-glide` has no musl/alpine wheels.
- Join the external `logus2k_network`; do not redeclare `valkey-bus` (owned by the agent_bus compose).
- Reuse, don't reinvent: the server-side function-calling loop is `noted`'s
  `backend/app/workflow/llm_dispatcher.py:dispatch_tool_calling`; RAG retrieve-then-inject is
  `cv/backend/main.py`; WhatsApp delivery is Socket.IO `/agent` `sendMessage` (see
  `whatsapp_agent/documents/whatsapp_bridge_sdk.md`). More pointers + ports are in this project's
  memory (`reference-patterns.md`).
- The user (António) performs **all git operations** — never commit/push/rebase.
- Prefer concrete vertical slices over up-front abstraction; verify load-bearing facts in code before
  asserting.
