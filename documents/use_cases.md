# Use Cases — composing agents on Agent Runtime

A "use case" is a **Project**: a node graph you author visually in Patron, deploy, and let the farm
run. Deploy compiles the composition to **one `GraphRecord`** plus **one firing binding**; when the
bound event fires, the farm routes it by `record_uid` and executes the graph from its entry node,
delivering the result to the destination(s).

```
Author in Patron → Deploy (compile to a GraphRecord + firing binding) → an event fires it → the farm runs it
```

> Schema and execution detail: [runtime_dsl_specification.md](runtime_dsl_specification.md) and
> [technical_architecture.md](technical_architecture.md). This document is the practical map of
> **what you can actually compose today** — every case below is verified against the live lowering
> and deploy-binding code.

---

## The one rule: a Project is ONE workflow

A Project deploys to a single graph with **one entry** and **one firing source**. Concretely:

- **One connected graph.** Two disconnected groups of blocks in the same Project → only the entry's
  group runs; the other is silently dead (deploy warns, doesn't refuse).
- **One firing source.** Multiple initiators → only **one** gets a firing binding (a schedule Trigger
  wins; otherwise the first File/Web/STT initiator). The rest never fire.

To run two independent workflows, make **two Projects**. Everything below assumes one connected graph
with a single initiator.

---

## The palette

| Family | Blocks | Notes |
|---|---|---|
| **Initiators** (fire the workflow) | Scheduled Trigger · File Initiator · Web Initiator · Speech-to-Text | one per Project |
| **Processing** | Agent · Vector Database · Graph Database | Agent capabilities (tools, RAG-pre, guardrails, skills, memory, loop) are **config on the Agent**, not separate blocks |
| **Destinations** (deliver) | WhatsApp · Text-to-Speech · Event Bus · File · Web | fan-out to several is allowed |

Disabled in the palette (planned, not yet runnable): **Data Transform**, **Workflow (composite)**.
Not exposed: **Branch**, **Loop-as-a-block**.

**The firing seed.** Each initiator seeds the workflow with the value the graph starts from:

| Initiator | Required config | Seed the Agent receives |
|---|---|---|
| Scheduled Trigger | `cron` (+ optional `task`) | the Trigger's `task` message |
| File Initiator | `watch_path` (default `/watched/in`) | **the file's contents** |
| Web Initiator | `route` (+ `method`) | the request body |
| Speech-to-Text | `stream_id` | the transcript |

An empty Agent `input_template` lets the seed flow in verbatim; a template weaves it via `{input}`.

---

## Use cases you can compose today

Each is verified to lower to a valid graph **and** establish a firing binding.

### 1. Scheduled digest → chat  *(the News Agent)*
`Scheduled Trigger → Agent (persona + MCP tool) → WhatsApp`
Every morning, curate headlines and post them. The agent advertises a tool (e.g. `newsapi_search`),
calls it, curates, and delivers. Fires on the scheduler cron.

### 2. Scheduled briefing → dashboard/stream
`Scheduled Trigger → Agent → Event Bus`
Same shape, delivered to a bus stream a dashboard observes instead of a chat.

### 3. Document intake
`File Initiator (/watched/in) → Agent → File Destination (/watched/out/…)`
Drop a file in the watched folder; the agent receives **its contents**, processes them, and the
result is written to the output folder. Fires on file create/modify.

### 4. Webhook agent
`Web Initiator (/route) → Agent → Web Destination`
A request hits the configured route; the agent acts on the body and the result is POSTed onward (or
returned via a Bus/File sink). Fires on the HTTP request.

### 5. Voice assistant
`Speech-to-Text → Agent → Text-to-Speech`
A transcript seeds the agent; the answer is synthesized back to speech. Fires when the STT front-end
emits a transcript on the configured `stream_id`.

### 6. Grounded Q&A  *(RAG-pre)*
`Scheduled/Web Initiator → Agent (rag_domains, rag_use_graph) → destination`
The Agent's RAG-pre config is **decomposed at deploy** into a `rag` node wired *before* the agent, so
retrieved passages are injected into the prompt. First-class grounding with no separate block.

### 7. Guardrailed agent
`… → Agent (guard_forbidden / guard_min_confidence) → destination`
Guardrail config is **decomposed at deploy** into a `guardrail` node wired *after* the agent, checking
output before delivery.

### 8. Standalone retrieval  *(no agent)*
`Scheduled/Web Initiator → Vector Database (or Graph Database) → destination`
Query a corpus / knowledge graph and deliver the results directly — a pure lookup, no LLM.

### 9. Retrieve-then-reason
`… → Vector Database → Agent → destination`
A standalone DB query feeds its results to an agent as input, which then reasons over them.

### 10. Broadcast  *(fan-out)*
`Agent → WhatsApp  +  Event Bus  +  File`
One node's output is broadcast to **every** connected destination — deliver the same result to several
channels at once.

**Agent capabilities (config, not blocks):** an Agent can also carry **tools** (an MCP allow-list),
**skills**, **memory** (thread window), and an outer **loop** (counter / expression / judge). These
lower onto the agent record and compose freely with the shapes above.

---

## Not composable yet (and the honest reasons)

| You might want… | Status | Why / what to do instead |
|---|---|---|
| **Reactive inbound chat** (a WhatsApp message triggers the agent) | ❌ dead | A Trigger set to `channel` type establishes **no firing binding** — it deploys but never fires. Use a **Web Initiator** or a **schedule** instead. |
| **Merge several firing sources** into one agent (fan-in) | ⚠️ partial | The graph lowers, but only **one** initiator gets a binding — the others never fire. Split into separate Projects. |
| **Conditional routing** ("if urgent → WhatsApp else File") | ❌ | The `Branch` block isn't exposed in the palette (and isn't wired into execution). |
| **Iteration as a block** | ❌ as a block | `Loop`-as-a-block isn't exposed — but the **Agent has a built-in loop** (counter/expression/judge) that covers most needs. |
| **Reshape data between blocks** | ❌ | `Data Transform` is disabled (planned). |
| **Nest a saved workflow as a block** | ❌ | `Workflow` (composite) is disabled (planned). |
| **Escalate to a stronger model / router** | ❌ | No router/chain block. |
| **Arbitrary custom code** | ❌ | No custom/script node (deliberately). |

---

## At a glance

| Capability | Composable in Patron now? |
|---|---|
| Scheduled / File / Web / Speech initiators | ✅ |
| Agent: persona, MCP tools, RAG-pre, guardrails, skills, memory, loop | ✅ (all Agent config) |
| Standalone Vector / Graph DB query | ✅ |
| Destinations: WhatsApp, TTS, Bus, File, Web | ✅ |
| Fan-out (broadcast to many destinations) | ✅ |
| Multi-stage chains (retrieve → reason → deliver) | ✅ |
| Reactive inbound chat (`channel` trigger) | ❌ never fires |
| Fan-in from multiple initiators | ⚠️ only one fires |
| Branch / Transform / Composite / Loop-block / router / custom | ❌ disabled or not exposed |

**Rule of thumb:** if it's a single connected pipeline — one initiator, any chain of Agent / DB /
retrieval blocks, fanning out to one or more destinations — it composes and runs today. Conditional
branching, data reshaping, sub-workflows, and reactive-chat triggers do not yet.
