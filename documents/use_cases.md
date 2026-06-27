# Use Cases — building agents on agent_runtime

The point of agent_runtime is that **a new agent is mostly a record, not code**. Once the node types it needs exist, you add an agent by writing a runtime DSL record (and creating any new preset/tool/domain it references). This doc shows the first agent end-to-end, then sketches a few more to illustrate how the **node catalog grows demand-driven** — each new agent pulls *at most one* new node into existence, after which the next agent of that shape is free.

> See [runtime_dsl_specification.md](runtime_dsl_specification.md) for the record schema and [technical_architecture.md](technical_architecture.md) for how it runs.

---

## 1. News Agent (the first inhabitant) — worked end-to-end

**Goal:** every morning, post a curated short list of headlines about a topic to a WhatsApp chat.

Why it's the first example: it's **proactive** (scheduled), which is the capability the bus + scheduler add and which no existing app had. It uses only nodes `trigger`, `brain`, `tools`, `delivery` — no RAG, memory, or guardrails — so it's the leanest real agent.

**What you create (three things):**

1. **A preset** in agent_server — `news_curator`:
   > *Given raw headlines on a topic, return the N best as deduped, ranked one-line headlines with links. No commentary, no preamble.*

2. **A scheduler job** (agent_scheduler) — fire daily, route to the farm:
   ```jsonc
   { "job_id": "news-morning-ai", "trigger_type": "cron",
     "trigger_args": { "cron_expression": "0 7 * * *" },
     "target_stream_id": "agent-runtime",
     "event_data": { "agent": "news-morning-ai" } }
   ```

3. **The agent record** (runtime DSL):
   ```yaml
   version: "0.1"
   id: news-morning-ai
   trigger:  { type: schedule }
   brain:    { persona: news_curator, llm: { temperature: 0.3, max_tokens: 1024 } }
   tools:    { server: noted, allow: [noted__newsapi_search, noted__fetch_url], max_rounds: 3 }
   input:    { template: "Curate the {n} best morning headlines about {topic}.", vars: { n: 5, topic: "AI agents" } }
   delivery: { channel: whatsapp, target: "351961050313@c.us" }
   ```

**What happens at 07:00:**

```
scheduler cron fires → XADD {agent: news-morning-ai} to stream:agent-runtime
agent_runtime consumes it → loads the record → runs the brain node:
   advertise [newsapi_search, fetch_url] to news_curator
   model calls newsapi_search(topic) → runtime invokes it via the noted MCP server
   result appended → model curates the short list (≤3 rounds)
deliver: connect WhatsApp bridge /agent, emit sendMessage(target, list)
emit run events to the bus (thought/tool/result) → visible in the console
```

**Efficiency:** one fire/day, one tool call + one curation call, one send — a near-fixed pipeline, bounded and cheap.

---

## 2. Sketches — how the next agents reuse or extend the catalog

Each adds **at most one** new node; afterward, agents of that shape are just records.

### a) Scheduled briefing to a dashboard *(reuses everything; new delivery target)*
Same shape as the News Agent, but `delivery.channel: bus` (post to a stream a dashboard observes) instead of WhatsApp. New agent = a record + a scheduler job. **No new node.**

### b) Grounded Q&A agent *(pulls in the `rag` node)*
A WhatsApp agent that answers from a knowledge base. Adds `rag` (rewriter preset + domains + graph) before the brain — the cv-style retrieve-then-inject. First such agent builds the `rag` node; every later grounded agent is then a record:
```yaml
trigger: { type: channel }
rag:     { rewriter: cv_query_rewriter, domains: [kb], use_graph: true }
brain:   { persona: kb_assistant }
delivery:{ channel: whatsapp, target: "<group-id>" }
```

### c) Action agent with a guardrail *(pulls in the `guardrail` node)*
An agent that proposes a command/action; a `guardrail` (Proxy) blocks forbidden patterns / low-confidence outputs before delivery. First such agent builds the `guardrail` node; the safety boundary is then reusable:
```yaml
brain:      { persona: ops_assistant }
tools:      { server: noted, allow: [noted__web_search], max_rounds: 4 }
guardrails: { forbidden: ["rm -rf", "DROP TABLE"], min_confidence: 0.6 }
delivery:   { channel: whatsapp, target: "<chat>" }
```

### d) Escalating agent *(pulls in a `chain`/`router` node, later)*
Try a fast local persona; if confidence drops, escalate the same context to a stronger model. This is the `chain`/`factory` node — added when an agent actually needs cost/quality routing, mirroring `noted`'s local→cloud cascade.

### e) The escape hatch *(`custom` node)*
An agent whose logic the catalog doesn't cover drops to a `custom` node referencing a registered handler — keeping you productive on the long tail without contorting the graph. Used sparingly; recurring `custom` logic is the signal to promote it into a real node.

---

## 3. The flywheel

| Agent | New node it adds | Cost of the *next* agent of that shape |
|---|---|---|
| News Agent | (bootstraps `trigger`/`brain`/`tools`/`delivery`) | a record + job |
| Dashboard briefing | none | a record + job |
| Grounded Q&A | `rag` | a record |
| Action agent | `guardrail` | a record |
| Escalating agent | `chain`/`router` | a record |

After the first few agents, the catalog covers the common patterns and new agents in that space drop to **a record (+ a preset/domain/tool if genuinely new)** — the "build agents fast" goal. The long tail rides the `custom` escape hatch; observability/eval (run traces on the bus, plus future `judge` nodes) keeps "fast to build" from outrunning "able to trust."
