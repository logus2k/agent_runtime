# Technical Architecture: agent_runtime

`agent_runtime` is the **execution runtime for declaratively-defined agents**. It hosts many agents in one lean process, consumes trigger events off `agent_bus`, and for each one runs the agent — delegating reasoning to `agent_server`, tools to the MCP server(s), retrieval to RAG, and delivery to a channel. It is the runtime end of an authoring→runtime split; it owns no cognition and little state.

---

## 1. Position in the ecosystem

agent_runtime is a **consumer** of the existing platform services; it depends on them, never the reverse.

```
patron (authoring, for humans)
   │  serialize
   ▼
COMPILER (reconcile + verify)         ← future; DSL is hand-written for now
   │  lower
   ▼
runtime DSL  (the contract, for the machine)
   │  interpret
   ▼
agent_runtime  ──────────────────────────────────────────────┐
   ├─ reasoning ........ agent_server (:7701, presets = personas, one shared model, FC + think/voice/answer)
   ├─ tools ............ MCP server(s) (e.g. noted :8123 `noted__*`, mcp-service :4950)
   ├─ retrieval ........ noted-rag (dense) + noted-graph (knowledge graph)
   ├─ triggers ......... agent_scheduler (:6816 → bus events) ; channels later
   ├─ transport ........ agent_bus (Valkey streams, envelope, consumer groups)
   └─ delivery ......... WhatsApp bridge (/agent Socket.IO), bus streams, TTS/avatar …
```

All on `logus2k_network`, one envelope contract, the same `auth-admin` gate for any admin surface.

---

## 2. The two-audience split (why the runtime DSL exists)

Two artifacts, two audiences:

- **patron's abstractions — for the user.** Optimized for recognizability, low cognitive load, good defaults, progressive disclosure. Tool-specific (litegraph) and free to evolve.
- **The runtime DSL — for the executor.** Optimized for explicitness, zero ambiguity, validatability, version stability. Tool-agnostic and semantic-only.

A **compiler** reconciles them (fills defaults, expands user conveniences, verifies) and **lowers** the authoring graph to the runtime DSL. Consequences this runtime relies on:

- **agent_runtime consumes only the runtime DSL.** Nothing patron/litegraph-shaped reaches it. In the first slice the DSL is hand-written; patron + compiler come later.
- **The two node catalogs need not be 1:1.** A friendly macro-node can lower to several runtime nodes — UX grows without new runtime primitives, and the runtime refactors without touching the palette.
- **Verification lives at compile time.** By the time the runtime receives the DSL it is already valid and canonical; the runtime executes, it does not re-validate authoring quirks. The runtime DSL *defines* the space of valid agents; patron is an ergonomic surface over it; the compiler keeps the surface ⊆ the space.

See [runtime_dsl_specification.md](runtime_dsl_specification.md) for the contract itself.

---

## 3. Execution model

**Agents are dormant config records; triggers activate transient tasks.** A scheduled agent does nothing 23h59m; on its trigger the runtime spins up an `asyncio` task that runs the pipeline and exits.

- **One lean async process, many agents.** Agents are **I/O-bound orchestrators** — the heavy compute lives in agent_server/MCP/RAG, reached over HTTP/Socket.IO. So a single event loop handles many concurrently as tasks; per-agent processes would burn memory for zero throughput. (A genuinely CPU-bound or untrusted agent graduates to an **ephemeral container** later — a substrate swap, since agents are config.)
- **Bounded concurrency.** A semaphore caps in-flight jobs at ≈ agent_server's slot count, so the farm never oversubscribes the shared brain.
- **Bounded jobs.** Each task carries a **timeout** and `try/except`; a hung/failed job can't take down the loop or its neighbors.
- **At-least-once + idempotency.** The bus may redeliver after a reclaim, so handlers dedupe on `cid`+`sid` (or `job_id`+`scheduled_run_time` for scheduler-origin events); crash recovery rides the bus's `XAUTOCLAIM` reaper.

This is the "agent farm": one process hosting many records, activating transient tasks on demand — high-throughput and cheap-at-rest.

---

## 4. The node model

An agent is a composition of **deterministic shells around a non-deterministic core** (the GoF-as-agentic framing from patron, used here as a *structural* lens, not a literal runtime). Each node is an **async adapter over a real component**:

| Node | Pattern | Backed by |
|---|---|---|
| `trigger` | Observer | a bus event (scheduler today; channels later) |
| `rag` | Builder | rewriter preset + noted-rag/noted-graph (optional) |
| `brain` | Factory / Strategy | an agent_server preset (runs the tool loop) |
| `tools` | Decorator | an MCP server (OpenAI tool specs + the FC loop) |
| `guardrail` | Proxy | local checks (forbidden patterns / min-confidence) |
| `delivery` | — | WhatsApp bridge, bus stream, TTS/avatar |

The catalog grows **demand-driven** — a node is added only when a real agent needs it. The node vocabulary is the runtime side; the user-facing palette (patron) can differ in granularity.

> **Pattern boundary = validation boundary.** Because LLM failure is probabilistic, the `guardrail` (Proxy) and any confidence-escalation (Chain) nodes are where hallucination blast radius is contained — they are first-class, not decoration.

---

## 5. The agentic loop

For a `brain` node with tools, the runtime runs a **server-side function-calling loop** — the proven pattern from `noted`'s `dispatch_tool_calling`:

```
advertise MCP tools (→ OpenAI specs) to the agent_server preset
loop ≤ max_rounds:
  POST /v1/chat/completions { model: preset, tools }
  if tool_calls: execute each via the MCP server → append role:'tool' results → continue
  else:          final content → done
```

agent_server emits **structured channels** that the runtime fans out:

- `<think>` → an `agent.thought` bus event (observable, never delivered)
- `<voice>` → an `agent.voice` event → TTS/avatar (when the delivery channel is voice)
- final answer → the deliverable

So one agent serves a text channel and a voice/avatar channel off the same call; the delivery node consumes the channel it needs.

Loop termination is **framework-enforced** (`max_rounds`, optional `stop_when`), aligned with agent_bus's termination guard — the runtime does not trust the model to stop.

---

## 6. Bus integration

agent_runtime is a **bus actor**:

- A **consumer group** on the farm's stream. `agent_scheduler` (an initiator) `XADD`s trigger events there; each event's `payload.data` carries `{ agent: <id>, …overrides }`.
- The runtime **resolves** the agent record by id, runs it, and **emits run events** back to the bus (`agent.thought` / `tool.exec` / `tool.result` / final / `workflow.terminated`) so every run is visible and replayable in the agent_bus console.
- This is the **first joint use of the bus + scheduler** by a real consumer — the bus is the runtime's eventing layer and, when a graph spans agents (Facade/multi-agent), its cross-agent transport.

---

## 7. Fault tolerance, security posture, and state

- **Fault tolerance:** per-job timeout + isolation; the bus's at-least-once delivery + reaper recover crashed/abandoned jobs; idempotency prevents double-effects (e.g. double-posting a message).
- **State:** the runtime is near-stateless. Conversation memory, when an agent needs it, lives in agent_server's `thread_window` keyed by the workflow id; durable workflow state lives in Valkey. The runtime holds only the (dormant) agent records.
- **Security (later, designed-for-not-yet):** secrets (e.g. WhatsApp tokens) come from config/secret resolution, never the DSL; untrusted or high-risk agents graduate to **ephemeral distroless** runs (strong isolation, minimal surface) — with a *constrained* spawner, since raw Docker-socket access would itself be a risk. Any admin surface sits behind the shared `auth-admin` gate.

---

## 8. Design principles (recap)

- Consume **only** the runtime DSL; keep authoring formats out of the runtime.
- Agents are **config**, not code; the marginal agent is a record once its nodes exist.
- **One async process, many dormant agents, transient bounded tasks.**
- **Guardrails are first-class** (probabilistic failure).
- **Stateless cognition, singular store.**
- **Escape hatch** (a `custom` node / flat record) for the long tail.
- **Demand-driven** node-catalog growth; let real agents pull nodes into existence.
