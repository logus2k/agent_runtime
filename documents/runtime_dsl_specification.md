# Runtime DSL Specification

**Status:** v0.1 (draft). The contract `agent_runtime` executes.

This is the **runtime DSL** — the tool-agnostic, versioned, semantic-only definition that the runtime interprets. It is the *machine-facing* abstraction; patron (the user-facing authoring tool) is a front-end that **compiles** to this. Authoring formats (litegraph, etc.) never reach the runtime.

> **Scope discipline.** The DSL describes **structure, parameters, and references** — which trigger, which preset, which tools, which guardrail, how nodes wire. It does **not** encode cognition: prompts live in `agent_server` presets; tool logic lives in MCP. Keep behavior out of the DSL or it drifts into specifying things it cannot enforce.

---

## 1. Two surface forms, one IR

| Form | For | Shape |
|---|---|---|
| **Flat record** | the linear ~80% of agents | a single object with optional stage fields |
| **Graph** | branching / multi-agent agents | nodes + typed edges (a DAG) |

Both **lower to the same IR**. A flat record is the degenerate linear graph. **v0 implements the flat record only**; the graph form is specified here for forward-compatibility (§6).

---

## 2. Flat record schema (v0)

All fields JSON/YAML; `version`, `id`, `trigger`, `brain`, `delivery` are required.

```yaml
version: "0.1"                 # DSL schema version (required)
id: news-morning-ai            # unique agent id (required; ^[A-Za-z0-9._:-]+$)
description: "Morning AI headlines to the ops chat"   # optional

trigger:                       # required — Observer
  type: schedule               # "schedule" | "channel" (channel = inbound message; later)

brain:                         # required — Factory/Strategy
  persona: news_curator        # agent_server preset name
  llm: { temperature: 0.3, max_tokens: 1024 }   # optional sampling overrides

tools:                         # optional — Decorator
  server: noted                # MCP server key (from the runtime's mcp config)
  allow: [noted__newsapi_search, noted__fetch_url]
  max_rounds: 3                # function-calling loop cap

rag:                           # optional — Builder (omit for tool-only agents)
  rewriter: cv_query_rewriter  # agent_server preset that formulates the query
  domains: [cv]                # RAG domain(s)
  use_graph: true

guardrails:                    # optional — Proxy
  forbidden: ["rm -rf", "DROP TABLE"]
  min_confidence: 0.5

input:                         # how the user/turn message is formed
  template: "Curate the {n} best morning headlines about {topic}."
  vars: { n: 5, topic: "AI agents" }

memory:                        # optional — Repository
  policy: none                 # "none" | "thread_window"
  max_turns: 20

delivery:                      # required
  channel: whatsapp            # "whatsapp" | "bus" | "tts" | …
  target: "351961050313@c.us"  # channel-specific destination
```

**Field reference**

| Field | Req | Notes |
|---|---|---|
| `version` | ✓ | DSL schema version; runtime rejects unknown majors. |
| `id` | ✓ | Stable id; also the routing key in trigger events. |
| `trigger.type` | ✓ | `schedule` (fired by an agent_scheduler bus event) or `channel`. |
| `brain.persona` | ✓ | agent_server preset; must exist (verified at compile time). |
| `brain.llm` | | Sampling overrides merged at request time. |
| `tools.server` / `allow` | | MCP server key + tool allow-list; `max_rounds` bounds the FC loop. |
| `rag.*` | | Enables retrieve-then-inject before the brain. Omit ⇒ no retrieval. |
| `guardrails.*` | | Output checks; a failure routes to a rejected/escalation path. |
| `input.template` / `vars` | | The user message; `{vars}` interpolated. Trigger events may override `vars`. |
| `memory.policy` | | `thread_window` maps the workflow id to agent_server's thread memory. |
| `delivery.channel` / `target` | ✓ | Where the result goes. |

---

## 3. Node catalog (v0)

Each node = a typed unit with **config**, **input ports**, **output ports**, backed by a real component. In the flat record these are implicit stages; in the graph form they are explicit nodes.

| Node | Pattern | In → Out (port types) | Config | Backing |
|---|---|---|---|---|
| `trigger` | Observer | — → `task` | `type` | bus event |
| `rag` | Builder | `task` → `context` | `rewriter`, `domains`, `use_graph` | rewriter preset + noted-rag/graph |
| `brain` | Factory/Strategy | `task`/`context` → `result` | `persona`, `llm`, (tools attached) | agent_server preset (FC loop) |
| `tools` | Decorator | (attaches to `brain`) | `server`, `allow`, `max_rounds` | MCP server |
| `guardrail` | Proxy | `result` → `result` (approved \| rejected) | `forbidden`, `min_confidence` | local checks |
| `delivery` | — | `result` → — | `channel`, `target` | WhatsApp bridge / bus / TTS |
| `custom` | escape hatch | `*` → `*` | `handler` ref | a registered handler |

Adding a node type is **demand-driven** — only when a real agent needs it. Candidates as the catalog grows: `factory`/`router` (model selection), `chain` (confidence escalation), `facade` (sub-agent orchestration), `judge`/`eval`.

---

## 4. Typed data shapes

The runtime passes typed payloads between stages; at component boundaries (and across the bus) they ride the agent_bus **envelope** (`payload.data`/`payload.context`).

- **`task`** — the work request: `{ id, instruction, vars, tags? }`.
- **`context`** — assembled prompt bundle (from `rag`): `{ task_id, prompt, sections[], est_tokens }`.
- **`result`** — execution output: `{ task_id, output, confidence?, ok, trace[] }`.

`trace[]` is append-only (which nodes touched the result) — the audit path surfaced in the bus console.

---

## 5. Validation & versioning

Enforced at **compile time** (so the runtime receives only valid DSL):

- **Schema:** required fields present; types correct; `id` pattern.
- **Reference resolution:** `brain.persona` and `rag.rewriter` exist on agent_server; `tools.allow` exist on the named MCP server; `delivery.channel` is supported.
- **Edge typing (graph form):** an output port may connect only to an input port of the same type (`*` wildcard for `custom`/inspectors).
- **Versioning:** `version` is `major.minor`. The runtime accepts known majors; the compiler migrates older minors forward. Breaking changes bump major.

The runtime re-checks only invariants it must (referenced agent exists at run time), not authoring quirks.

---

## 6. Graph form (forward-looking)

```jsonc
{
  "version": "0.1",
  "id": "research-supervisor",
  "nodes": [
    { "id": "t",  "type": "trigger",   "config": { "type": "channel" } },
    { "id": "r",  "type": "rag",       "config": { "domains": ["kb"], "use_graph": true } },
    { "id": "b",  "type": "brain",     "config": { "persona": "researcher" } },
    { "id": "g",  "type": "guardrail", "config": { "min_confidence": 0.7 } },
    { "id": "d",  "type": "delivery",  "config": { "channel": "whatsapp", "target": "…" } }
  ],
  "edges": [
    { "from": "t.task", "to": "r.task" },
    { "from": "r.context", "to": "b.context" },
    { "from": "b.result", "to": "g.result" },
    { "from": "g.approved", "to": "d.result" }
  ]
}
```

A flat record **desugars** to this: each present stage field becomes a node, wired in the canonical order `trigger → rag? → brain → guardrail? → delivery`. So the runtime interprets *one* shape; the flat record is authoring sugar.

---

## 7. Canonical example — the News Agent

```yaml
version: "0.1"
id: news-morning-ai
trigger:  { type: schedule }
brain:    { persona: news_curator, llm: { temperature: 0.3, max_tokens: 1024 } }
tools:    { server: noted, allow: [noted__newsapi_search, noted__fetch_url], max_rounds: 3 }
input:    { template: "Curate the {n} best morning headlines about {topic}.", vars: { n: 5, topic: "AI agents" } }
delivery: { channel: whatsapp, target: "351961050313@c.us" }
```

Fired daily by an `agent_scheduler` cron job whose event carries `{ agent: "news-morning-ai" }`. See [use_cases.md](use_cases.md).
