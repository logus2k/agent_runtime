# Design: Multi-agent Workflow Execution

**Status:** Design (2026-07-01). **Orthogonal** to the [Resource Model](../resource_model.md) —
this is runtime execution, not management. Captured so it isn't lost.

## Real today vs the gap
**REAL (proven with fakes):** `GraphExecutor.run` (`composer/executor.py`) already walks a general
`IRGraph` by real out-edges, picks ports from `StepResult`, and handles linear/branch/loop/
step-budget/dead-end. `test_composer_executor.py` proves branch routes data-dependently and loop
repeats+exits; `composite_handler` nesting proven in `test_composer_composite.py`. `IRGraph`/
`IRNode`/`IREdge` are a real DAG. The runner already executes the linear agent *through* the
executor (`runner.py::ir_from_record` + `h_trigger`/`h_agent`/`h_deliver`).

**The GAP (not the executor):**
- `lower.py` produces only a **linear** result — `Graph.lower()` hard-requires `len(agents)==1`
  and `_trace_to_destination` rejects >1 out-link and any non-`transform` node between agent and
  destination. A 2-agent or agent→branch graph **can't even lower today**.
- `runner.py::ir_from_record` builds IR from a **flat `AgentRecord`** (one brain, one delivery,
  `extra="forbid"`) — a multi-agent workflow has **no representation** in `AgentRecord`.
- `Branch/Loop/Composite.lower()` return `{}` ("graph-form only") — routing works in the executor
  but the *decision logic* (predicate/condition) is a fake in every current test.

**Key decision:** do NOT widen `AgentRecord` (it's fundamentally single-agent). Add a **separate
graph-form path**: lower the serialized composer graph directly to an `IRGraph` (general topology
walk) with **per-node** config, and let the runner execute an `IRGraph` whose nodes carry their own
brain/delivery config.

## First vertical slice
**agent → agent chain** (planner → writer → one destination). Exercises the true gap (multiple
agents, data flowing edge-to-edge: planner's answer becomes the writer's task) while reusing every
handler mechanism and needing **zero executor changes** (all edges are plain `"out"`). Branch→2-
destinations is a good *second* slice (adds port-routing on the lowering side); start with the chain.

## New code
1. **`Graph.to_workflow_ir()`** in `lower.py` (the load-bearing new piece): validate one trigger,
   allow **N agents** + arbitrary destinations; BFS/DFS from the trigger over `self.links`, emit one
   `IRNode` per node (kind = node type) + one `IREdge` per link (port = out-slot label); store each
   block's `lower()` fragment in `IRNode.config`. Returns an `IRGraph` with `entry = trigger id`.
   Keep the existing linear `lower()`/`to_ir()` untouched (back-compat with the live News Agent +
   `test_composer_lower.py`).
2. **Per-node handlers** in `runner.py`: `h_agent(node, value, ctx)` reads config from `node.config`
   (not a closed-over single `record`) — build a per-node `AgentRecord`-shaped view and call
   `run_brain(...)` with the incoming `value` as the task. Cleanest minimal change: each agent
   `IRNode.config` carries a fully-formed `AgentRecord`, so `h_agent` stays byte-near-identical.
   Add a `run_workflow(ir, env)` entrypoint. Wire `on_trace` (`executor.py`) to emit an
   `edge.traversed` run event — where the Edge/envelope contract becomes load-bearing at runtime.

## Test slice
- **Lowering**: 2-agent fixture → `to_workflow_ir()` → assert 4 IR nodes, edges
  `trigger→a1→a2→dest`, entry=trigger, each agent node carries its own persona/input fragment.
- **Executor routing** (fakes): fake handlers record node order → `[trigger, agent:2, agent:3,
  whatsapp:4]`; fake agent1 returns `"PLAN"`, assert agent2 received `"PLAN"` as incoming value.
- **Runner e2e** (`test_runner_graph.py` style, FakeAgentServer/FakeBus): scripted 2 responses;
  assert 2 agent_server calls, call 2's task contains call 1's answer (edge-to-edge proven),
  delivery carries the final answer, `agent.result`+`workflow.terminated` (+ optional
  `edge.traversed`) events emitted.

## Risks / honest limits
- **Executor needs no change** — the "only linear proven" caveat is about `lower.py`+`runner.py`.
- **Per-node config is the real work** — today's runner is single-`record` throughout
  (`_build_task`/`_make_mcp`/`h_agent`). First slice: use **tool-less** agents (personas only) to
  dodge per-node MCP client construction; generalize after.
- **True parallel fan-out is unmodelled** — the executor rejects >1 edge on one port. agent→branch→2
  works only because a branch picks **one** port at runtime (mutually-exclusive routing). Real
  parallel fan-out (both destinations) needs new parallel-construct work.
- **Branch/Loop predicate logic is aspirational** — routing is real, the decision engine is a fake.
  Irrelevant to the agent→agent slice (another reason to start there).

## Critical files
`composer/lower.py` (add `to_workflow_ir`) · `runner.py` (per-node handlers + graph entrypoint +
`on_trace`) · `composer/executor.py` (reference — already general) · `composer/blocks.py` (per-block
`lower()` reused for node config) · `tests/test_runner_graph.py` (multi-node e2e pattern).
