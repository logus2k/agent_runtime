# Implementation Plan: agent_runtime

**Status:** draft for review. First vertical slice = the **News Agent**.

## What this is

`agent_runtime` is the **runtime that hosts many agents and executes the compiled runtime DSL**. It's the execution end of the authoring→runtime split:

```
patron (author, for humans) ─► COMPILER (reconcile + verify) ─► runtime DSL (for the machine) ─► agent_runtime (execute)
```

It is a **bus actor**: it consumes trigger events (from `agent_scheduler` today, channels later) off `agent_bus`, and for each one runs an agent — delegating reasoning to `agent_server` (personas/presets), tools to the MCP server(s), retrieval to RAG, and delivery to a channel (WhatsApp bridge first). It owns no cognition and little state; agents are **dormant config records**, activated into **transient async tasks** on trigger.

> Hard boundary: **nothing patron/litegraph-shaped ever reaches this runtime.** It consumes only the runtime DSL (or its flat form). In this first slice the DSL is hand-written; patron + the compiler come later.

## Design principles (carried from the design discussion)

- **One lean async process, many agents.** Agents are I/O-bound orchestrators (the heavy work is in agent_server/MCP/RAG), so a single event loop runs many concurrently as tasks. No process-per-agent.
- **Dormant records → transient tasks.** An idle agent costs ~nothing; a trigger spins up a task that runs the pipeline and exits. Concurrency bounded by a **semaphore ≈ agent_server's slot count**; per-job **timeout** + `try/except`; rely on the bus's at-least-once + reaper for crash recovery.
- **Pattern boundary = validation boundary.** Guardrails are a first-class node, not an afterthought (failure is probabilistic).
- **Stateless cognition, singular store.** Conversation state (when needed) lives in agent_server's `thread_window` / Valkey, not in the runtime.
- **Escape hatch.** A `custom` node (or a flat hand-written record) carries the 20% the node catalog doesn't cover.
- **Demand-driven catalog.** Add node types only as real agents need them.

## The runtime DSL (v0)

JSON/YAML, schema-validated (Pydantic), **versioned**. The News Agent is linear, so v0 is a **flat record** that lowers to a node graph later — same runtime, simplest authoring surface first.

```yaml
version: "0.1"
id: news-morning-ai
trigger:   { type: schedule }              # Observer — fired by an agent_scheduler bus event
brain:     { persona: news_curator,        # agent_server preset (Factory/Strategy)
             llm: { temperature: 0.3, max_tokens: 1024 } }
tools:     { server: noted,                # Decorator — MCP server + allow-list
             allow: [noted__newsapi_search, noted__fetch_url],
             max_rounds: 3 }
rag:       null                            # Builder — none for news (live tool query, not a corpus)
guardrails: null                           # Proxy — optional
input:     { template: "Curate the {n} best morning headlines about {topic}.",
             vars: { n: 5, topic: "AI agents" } }
delivery:  { channel: whatsapp, target: "351961050313@c.us" }
```

The **node vocabulary** (the ~6 we need first; each is an async adapter over a real component):

| Node | Pattern | Backed by | Notes |
|---|---|---|---|
| `trigger` | Observer | bus event (scheduler / channel) | entry point |
| `rag` | Builder | rewriter preset + noted-rag/graph | optional |
| `brain` | Factory/Strategy | agent_server preset | runs the tool loop |
| `tools` | Decorator | MCP server | OpenAI tool specs + FC loop |
| `guardrail` | Proxy | local checks | forbidden patterns / min-confidence |
| `delivery` | — | WhatsApp bridge `/agent` | `sendMessage` |

Typed ports (`task`/`context`/`result`) map to real payloads in-process and to the **bus envelope** at component boundaries. Keep the DSL describing **structure + parameters + references only** — never prompts (those live in agent_server presets).

## The executor

1. **Bus consumer** (built on the agent_bus patterns): a consumer group on the farm's stream. `agent_scheduler` jobs target that stream; each event's `payload.data` carries `{ agent: <id>, ...overrides }`.
2. **Resolve** the agent record by id from the local registry (`data/agents/*.yaml`).
3. **Dispatch** a bounded transient task (semaphore + timeout). Idempotency: dedupe on `cid`+`sid` (or `job_id`+`scheduled_run_time` for scheduler-origin).
4. **Run the graph.** For the News Agent (linear): the `brain` node runs a **server-side function-calling loop** — advertise the MCP tools (fetched from the MCP server, converted to OpenAI specs) to the `news_curator` preset, execute returned `tool_calls` against the MCP server, append `role:'tool'` results, loop ≤ `max_rounds`, take the final content. *(This is exactly noted's `dispatch_tool_calling` pattern — reference, don't reinvent.)*
5. **Guardrail** (none here) → **deliver**: connect to the WhatsApp bridge `/agent` Socket.IO namespace with the agent's token and `emit('sendMessage', { targetId, text })`.
6. **Observability:** emit run events to the bus (`agent.thought` / `tool.exec` / `tool.result` / final / `workflow.terminated`) so runs show up in the agent_bus console.

## News Agent — the first inhabitant

- **Preset:** `news_curator` in agent_server (system prompt: given raw headlines on a topic, return N deduped, ranked one-line headlines + links; no commentary). Two files / admin API.
- **Scheduler job:** cron `0 7 * * *`, `target_stream` = the farm stream, `event_data: { agent: "news-morning-ai" }`. (Scheduler stays dumb — "run agent X"; the record holds topic/target/tools.)
- **Agent record:** the YAML above.
- **Pipeline:** `newsapi_search` (via MCP) → curate (`news_curator`) → format → `sendMessage` to the chat.
- **Why it's efficient:** one fire/day, one tool call + one curation call (a near-fixed pipeline, ≤3 rounds), one send. Bounded and cheap.

## Build steps

- **Step 0 · Skeleton.** `python:3.12-slim-bookworm` (glibc), `requirements.txt`, `docker-compose.yml` on `logus2k_network` (no redeclare of `valkey-bus`), `config.py`, `/health`. Depends on valkey-bus + agent_server + the MCP server + the WhatsApp bridge being reachable. **Done when:** container is up and healthy.
- **Step 1 · Runtime DSL.** Pydantic models for the agent record + loader + validation (versioned). **Done when:** a malformed record is rejected with a clear error; the News record loads.
- **Step 2 · Bus consumer + dispatch.** Consumer-group loop, agent registry, semaphore-bounded transient tasks, timeout + error handling, idempotency. **Done when:** a synthetic trigger event runs a no-op agent end to end with one task.
- **Step 3 · Node adapters.** Implement `brain` (the FC tool loop over agent_server + MCP), `tools` (MCP client: list→OpenAI specs, invoke), `guardrail`, (`rag` deferred). **Done when:** the `brain` node curates real `newsapi_search` output for a topic.
- **Step 4 · Delivery adapter.** WhatsApp bridge `/agent` Socket.IO client (`sendMessage`), token from the record/secret. **Done when:** the runtime posts a test message to a chat.
- **Step 5 · News Agent wiring.** Create the `news_curator` preset + the scheduler job + the record; run end to end. **Done when:** a scheduler `run-now` delivers a curated headline list to the chat.
- **Step 6 · Observability.** Emit run events to the bus so the console shows agent runs. **Done when:** a run is visible as a workflow on the bus.

## Testing

- **Unit:** DSL schema validation; each node adapter with fakes (no live services).
- **Integration (live stack):** scheduler fires → runtime runs → message delivered; verify timeout/error paths and idempotency (no double-post on redelivery).
- **Manual:** scheduler `run-now`; observe WhatsApp delivery + the bus console trace.

## Open decisions (confirm before/while building)

1. **Bus client: vendor vs shared package.** `agent_scheduler` vendored a thin glide *publisher* + a copy of `envelope.py`. `agent_runtime` needs *consumer* ops too (`xreadgroup`/`xack`/`ensure_group`/`xautoclaim`). Options: (a) vendor a thin bus-consumer client + envelope (fastest, consistent with the scheduler, but more copy-drift), or (b) extract agent_bus's bus client + envelope into a shared installable package both import (cleaner; also fixes the scheduler's vendored-envelope drift). **Recommend:** start (a), plan (b) as the consolidation. *(This is the third place the envelope has been copied — a real signal for (b).)*
2. **Stream topology.** One shared `agent-runtime` stream the farm consumes, routing by `payload.data.agent` (recommended — simplest), vs per-agent streams.
3. **Agent registry source.** Local `data/agents/*.yaml` for now (matches scheduler/whatsapp convention), later fed by the compiler / an admin surface.
4. **Reuse the agent_bus actor framework** (BaseActor/guard/reaper) vs a focused bus consumer. Recommend a focused consumer for v0, reusing the *patterns* (idempotency, reaper-style reclaim) without importing the framework until (b) above happens.
5. **Tool execution path.** Confirm the MCP surface to call: the whatsapp config points at `noted` MCP (`http://noted:8123`, tools `noted__*`); confirm whether to call its MCP `tools/call` or a REST `/invoke`, and how to fetch+convert specs to OpenAI format.

## Out of scope (later, demand-driven)

- **patron + the compiler** (DSL is hand-written for now).
- The **full node catalog** / GoF patterns beyond the ~6; **eval/judge** nodes (needed as the catalog grows — flag, don't build yet).
- **Ephemeral-container substrate** (graduate a hot/untrusted agent later — it's a substrate swap, agents are config).
- **Multi-agent / Facade / supervisor** graphs; reactive channel triggers (inbound WhatsApp/voice) beyond the scheduled case.
