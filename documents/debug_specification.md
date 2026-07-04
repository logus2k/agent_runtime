# Debug (Step-by-Step Execution) — Implementation Specification

**Status: DESIGNED, not yet implemented.** The Trace panel (the *observe* half) is built and live —
this spec adds the *control* half: pause a run, advance it node-by-node, inspect the payload at
each step, continue, or stop.

This document is grounded in the code as it stands (verified 2026-07-04). File/function anchors are
real; where it says "add", nothing exists yet.

---

## 1. Purpose & scope

The Trace panel already streams every workflow step + the payload between blocks
(`edge.traversed`, `agent.result`, `workflow.terminated`) live over SSE. Debug turns that
read-only view into an **interactive debugger**:

- **Run in debug mode** — a run that pauses *before each node* instead of running to completion.
- **Step** — execute exactly one node (run its handler, fan out its output), then pause again.
- **Continue** — release the run to finish normally (stop pausing).
- **Stop** — abort the paused run cleanly.
- **Inspect** — at each pause, the UI shows which node is next and the incoming payload it will
  receive (the Trace panel already renders payloads; Debug adds the *pause point*).

**In scope (v1):** per-node pause/step/continue/stop for a single deployed graph run, driven from
the Trace panel, owner-gated. **Out of scope (v1):** breakpoints on selected nodes, editing the
payload mid-run, back-stepping, and debugging concurrent fan-in branches independently (see §10).

---

## 2. What already exists (the seams Debug hooks into)

All of these are built and working today:

- **The executor loop** — `GraphWorkflowExecutor.run()` in
  [src/agent_runtime/graph_executor.py](../src/agent_runtime/graph_executor.py). A `deque` of
  `_Msg(node_id, value)`; each iteration pops one message, runs `handler(node, value, ctx)` **once**
  (per-message fan-in, no barrier), then fans the output out to every successor edge, calling
  `await self._on_trace(edge.src, edge.dst, edge.port, out_value, ctx)` per edge. **This one-node-
  per-iteration loop is the natural step boundary.**
- **`WalkContext`** (same file) — threaded through every handler; carries `cid` and a `scratch`
  bag. The debug session handle rides here (§4.1).
- **The Runner** — `Runner.run_graph_record(record, env)` in
  [src/agent_runtime/runner.py](../src/agent_runtime/runner.py) builds the handler set + an
  `emit_for()` that stamps `record_uid` on **every** run event, constructs
  `GraphWorkflowExecutor(handlers, on_trace=on_trace)`, and runs it. It reads the seed via
  `seed_of(env)` from `env.payload.data`.
- **The event hub** — [src/agent_runtime/events.py](../src/agent_runtime/events.py): a process-global
  in-memory fan-out (`hub`). The Runner's `_emit` publishes here; the SSE endpoint subscribes.
- **Manual fire** — `POST /admin/projects/{uid}/fire` (`fire_project`,
  [admin.py](../src/agent_runtime/admin.py)) with body `{task}`. Mints a fresh `cid`, publishes a
  `console.fired {record_uid, task}` envelope to the farm stream; the farm routes by `record_uid`
  and dispatches it as a bounded async task → `run_graph_record`. Returns `{ok, uid, cid, entry}`.
- **The live event stream** — `GET /admin/projects/{uid}/events` (`project_events`), owner-gated,
  subscribes a hub queue, and forwards **every** event whose `data.record_uid == uid` as an SSE
  frame `{event, cid, ...data}`.
- **The farm dispatch** — `farm._handle(delivery)` in
  [src/agent_runtime/farm.py](../src/agent_runtime/farm.py) resolves `data.record_uid` → the
  `GraphRecord` and calls the routed `run_graph_record`. Jobs are **bounded by a timeout** (§4.6).
- **Multi-tenancy** — `require_access(request, rec.owner)` gates fire/events/status; owner = OIDC
  `sub`, admins matched by email or sub. Debug's control endpoints reuse this verbatim.
- **Patron** — [js/trace-panel.js](../../patron/js/trace-panel.js) (`window.PatronTrace`, fed by the
  per-project `EventSource` in [js/app.js](../../patron/js/app.js)); fire is `POST
  api/projects/<uid>/fire` relayed by `patron/serve.py`.

Key consequence: **the run executes inside the farm process as an async task**. Pausing it means an
`await` inside `GraphWorkflowExecutor.run` that blocks on a per-run signal an HTTP endpoint sets.

---

## 3. The model

A **DebugSession** is created when a run is fired with `debug: true`. It is keyed by the run's
**`cid`** (one session per run; runs are already cid-isolated). It holds a pause-gate the executor
awaits before each node, plus the run's `uid`/`owner` for authorization. A process-global
**DebugRegistry** maps `cid → DebugSession`, mirroring the `hub` singleton pattern.

Control flow:

```
fire(debug=true) ── farm dispatch ──► run_graph_record
                                         │ creates DebugSession(cid, uid, owner), registers it
                                         ▼
   GraphWorkflowExecutor.run loop, per popped node:
     await session.gate(node)  ──►  mode 'step'     : emit node.paused, block until a step token
                                    mode 'continue' : return immediately (run to completion)
                                    mode 'stopped'  : raise DebugStopped → clean abort
     run handler, fan out (edge.traversed as today)
   run ends ──► registry.remove(cid)

POST /step      → session.step()      (release one node)
POST /continue  → session.cont()      (stop pausing; finish the run)
POST /stop      → session.stop()      (abort at the next gate)
```

Because the gate sits **before** `handler(...)`, a Step runs exactly one node and then re-pauses at
the next popped message — which, with fan-out/fan-in, is precisely "one block at a time" in queue
order.

---

## 4. Backend design

### 4.1 DebugSession + DebugRegistry — new `src/agent_runtime/debug.py`

```python
class DebugStopped(Exception):
    """Raised inside the executor when a debug run is asked to stop — a clean abort."""

class DebugSession:
    cid: str
    uid: str                 # the deployed record uid (for owner-gating the control endpoints)
    owner: Optional[str]     # record owner (sub) captured at creation
    mode: str                # 'step' | 'continue' | 'stopped'
    _tokens: asyncio.Queue   # one token released per Step
    paused_node: Optional[str]

    async def gate(self, node_id, kind, incoming, emit) -> None:
        """Called by the executor BEFORE running each node.
        - 'continue' → return at once.
        - 'stopped'  → raise DebugStopped.
        - 'step'     → emit a `node.paused` event, then await a single step token.
        """
    def step(self):   ...  # put one token (advance one node)
    def cont(self):   ...  # mode='continue' + release any current await
    def stop(self):   ...  # mode='stopped'  + release any current await
    def touch(self):  ...  # update last-activity ts for the idle reaper (§4.6)

class DebugRegistry:
    def create(cid, uid, owner) -> DebugSession
    def get(cid) -> Optional[DebugSession]
    def remove(cid) -> None

registry = DebugRegistry()   # process-global singleton (import-shared, like events.hub)
```

`gate()` is the whole mechanism. Implementation sketch:

```python
async def gate(self, node_id, kind, incoming, emit):
    self.touch()
    if self.mode == "stopped":
        raise DebugStopped(self.cid)
    if self.mode == "continue":
        return
    self.paused_node = node_id
    await emit("node.paused", {"node": node_id, "kind": kind,
                               "incoming": _preview(incoming)})   # reuse runner._preview cap
    await self._tokens.get()          # blocks until step()/cont()/stop() releases it
    if self.mode == "stopped":
        raise DebugStopped(self.cid)
```

`cont()`/`stop()` set the mode then `self._tokens.put_nowait(1)` so a currently-awaiting `gate`
unblocks and re-reads the mode.

### 4.2 Executor pause-gate — `graph_executor.py`

`GraphWorkflowExecutor.__init__` gains an optional `debug=None` (a `DebugSession`) and an
`on_pause` async callback (so the executor stays free of admin/event imports — the Runner supplies
the emit closure). In `run()`, **immediately after resolving `handler` and before calling it**:

```python
if self._debug is not None:
    await self._debug.gate(node.id, node.kind, msg.value, self._on_pause)
out_value = await handler(node, msg.value, ctx)
```

`DebugStopped` propagates out of `run()`; the Runner catches it (§4.3) and emits
`workflow.terminated {reason: "debug-stopped"}`. No handler code changes; `on_trace` /
`edge.traversed` stay exactly as they are (a Step still emits the edge payloads after the node runs).

### 4.3 Starting a debug run — `runner.run_graph_record`

The fired event's `data` gains an optional `debug: true` (§4.5). In `run_graph_record`, after
`seed_of(env)`:

```python
debug_flag = bool((env.payload.data or {}).get("debug"))
session = registry.create(cid, record.uid, record.owner) if debug_flag else None
executor = GraphWorkflowExecutor(handlers, on_trace=on_trace,
                                 debug=session,
                                 on_pause=lambda et, d: emit_for(None, et, d))
try:
    await executor.run(record, None, walk_ctx)
except DebugStopped:
    await emit_for(None, "workflow.terminated", {"reason": "debug-stopped"})
finally:
    if session is not None:
        registry.remove(cid)
```

`emit_for` already stamps `record_uid` on every event, so `node.paused` flows to the Trace panel
through the **existing** SSE path with no endpoint change.

### 4.4 New run events (carried by the existing SSE frame `{event, cid, ...data}`)

- **`node.paused`** `{record_uid, cid, node, kind, incoming}` — the executor is paused *before*
  running `node`; the UI shows the pause point + the payload it's about to receive.
- **`workflow.terminated`** already exists; Debug adds the reasons `"debug-stopped"` and
  (from §4.6) `"debug-timeout"`.
- (Optional) **`node.stepped`** `{node}` after a successful step — usually unnecessary, since
  `edge.traversed` + the next `node.paused` already convey progress.

### 4.5 Control endpoints — `admin.py` (owner-gated, mirrors `fire_project`)

- `POST /admin/projects/{uid}/fire` — extend `_FireBody` with `debug: bool = False`; pass it into
  `fired_event`'s `event_data` (add a `debug` kwarg to the `agent-bus-client` `fired_event`, or set
  it on the envelope's `payload.data` before publish). Everything else about fire is unchanged.
- `POST /admin/projects/{uid}/step`   body `{cid}` → resolve record, `require_access(rec.owner)`,
  `session = registry.get(cid)`; 404 if no session, 409 if `session.uid != uid`; else
  `session.step()`; return `{ok, cid, paused_node}`.
- `POST /admin/projects/{uid}/continue` body `{cid}` → `session.cont()`.
- `POST /admin/projects/{uid}/stop`     body `{cid}` → `session.stop()`.
- (Optional) `GET /admin/projects/{uid}/debug?cid=…` → `{active, mode, paused_node}` for a UI that
  reconnects mid-run.

Authorization: identical to `fire`/`events` — look up the record, `require_access(request,
rec.owner)`. Additionally assert `session.uid == uid` so a cid can only be driven through its own
project's endpoint. `patron/serve.py` adds three relay routes
(`/api/projects/<uid>/{step,continue,stop}` → farm) using `_farm_headers()` (same pattern as
`_project_fire`).

### 4.6 Lifecycle & safety

- **Bounded-task timeout.** The farm dispatches runs with a timeout (`farm.py`). A paused debug run
  would trip it. Resolution: when a run is in debug mode, the farm must **not** apply the normal
  job timeout — either mark debug deliveries exempt (the fired event carries `debug: true`, which
  `_handle` can read) or give them a large debug ceiling. **Decision needed** (§7.1).
- **Idle reaper.** A `DebugSession` tracks last-activity (`touch()` on gate + each control call). A
  reaper (reuse the farm reaper loop, or a small task) auto-**stops** a session idle > `DEBUG_IDLE_
  TIMEOUT` (default 5 min) so an abandoned paused run can't leak a task forever → emits
  `workflow.terminated {reason: "debug-timeout"}`.
- **Panel disconnect.** Closing the Trace panel drops the SSE; it does **not** by itself stop the
  run. The idle reaper is the backstop. (v2 could auto-continue on disconnect — see §7.2.)
- **Cleanup.** `registry.remove(cid)` in the Runner's `finally` guarantees the session is gone when
  the run ends by any path (done / stopped / error / timeout).
- **Cap unchanged.** The executor's `max_steps` budget still applies — debug doesn't disable the
  runaway-cycle backstop.

---

## 5. Frontend design (Patron)

All additions live in [js/trace-panel.js](../../patron/js/trace-panel.js) + a fire entry point; the
event plumbing (`EventSource` → `PatronTrace.push`) is reused unchanged.

- **Start a debug run.** Add a control that fires with `debug: true`. Options (pick in §7.3):
  (a) a "Debug ▸ Step Run" toggle beside the Console Send button, (b) a small "Fire (debug)" input
  in the Trace panel header, or (c) a `Build ▸ Debug Run` menu command. All call `POST
  api/projects/<uid>/fire {task, debug:true}` and remember the returned `cid`.
- **Pause banner + controls.** On a `node.paused` frame for the active `cid`, the Trace panel shows
  a sticky bar: `⏸ paused before <node> (<kind>)` + **Step** / **Continue** / **Stop** buttons that
  POST `{cid}` to the matching endpoint. Buttons disable while a request is in flight.
- **Payload inspection.** `node.paused.incoming` is shown in the bar (and the run's row list already
  shows `edge.traversed` payloads for completed steps) — so the user sees *what's about to enter*
  the paused node and *what flowed* between prior nodes.
- **Canvas highlight (nice-to-have).** Flash/outline the paused litegraph node (the id is in
  `node.paused.node`; `graph.getNodeById` already used by the Console Receive path) so the pause
  point is visible on the graph, not just in the panel.
- **End states.** On `workflow.terminated`, clear the pause bar and show the reason
  (done / debug-stopped / debug-timeout).

---

## 6. Multi-tenancy & isolation

- Control endpoints are owner-gated exactly like fire/events (`require_access(request, rec.owner)`),
  so a user can only step/continue/stop **their own** project's runs. `session.uid == uid` prevents
  driving a cid through another project's path.
- Sessions are cid-keyed and cid is run-isolated, so concurrent debug runs (even of the same graph)
  don't interfere.
- `/events` is already owner-gated, so the `node.paused` frames only reach the owner.

---

## 7. Open decisions

1. **Debug timeout treatment** (§4.6) — exempt debug deliveries from the farm job timeout, or give
   them a large fixed ceiling? *Recommendation: exempt + rely on the idle reaper (5 min).*
2. **Disconnect behavior** — on SSE drop, leave paused (reaper stops it) vs auto-continue?
   *Recommendation: leave paused; the reaper is the backstop; add auto-continue only if users ask.*
3. **Where "start debug" lives in the UI** (§5) — Console Send adjunct vs Trace-panel control vs
   Build menu. *Recommendation: a Trace-panel "Fire (debug)" control, so debugging is self-contained
   in the panel that shows the result.*
4. **Step granularity** — per-node (this spec) vs per-edge. *Recommendation: per-node; it matches the
   executor loop and reads naturally as "one block at a time."*
5. **Breakpoints** (pause only at chosen nodes) — v2. v1 pauses at every node in step mode.

---

## 8. Phased implementation plan

Each phase is independently testable; land in order.

- **Step 0 — `debug.py`**: `DebugSession`, `DebugRegistry`, `DebugStopped`, `registry` singleton.
  Unit tests for step/continue/stop token flow (no executor).
- **Step 1 — executor gate**: add `debug`/`on_pause` to `GraphWorkflowExecutor`; await
  `session.gate(...)` before each handler; propagate `DebugStopped`. Test with fake handlers that a
  step advances exactly one node and stop aborts.
- **Step 2 — runner wiring**: read `data.debug`, create/register/remove the session, emit
  `node.paused` via `emit_for`, catch `DebugStopped`. Test the full `run_graph_record` in step mode
  emits `node.paused` and advances on `step()`.
- **Step 3 — endpoints**: `_FireBody.debug`; `/step`,`/continue`,`/stop` (owner-gated, `session.uid`
  check); `patron/serve.py` relays. CI tests (TestClient + fake bus): non-owner → 403; step advances;
  stop → `workflow.terminated{reason:"debug-stopped"}`.
- **Step 4 — Trace panel UI**: fire-in-debug control, pause bar + Step/Continue/Stop, payload
  display. Live-verify against a deployed graph (rebuild patron, drive with Playwright / manual).
- **Step 5 — safety + polish**: farm timeout exemption, idle reaper (`DEBUG_IDLE_TIMEOUT`), canvas
  node highlight.

---

## 9. Testing (CI-friendly)

Mirror the existing suites (FastAPI `TestClient` + in-memory fakes, no containers — Jenkins-ready,
see `tests/test_multi_tenancy.py`, `tests/test_project_status.py`):

- `test_debug_session.py` — step releases exactly one token; continue/stop set mode + unblock a
  pending `gate`; a stopped gate raises `DebugStopped`.
- `test_debug_executor.py` — a `GraphWorkflowExecutor` with a `DebugSession` in step mode runs zero
  nodes until `step()`, one node per `step()`, and aborts on `stop()`; a `continue` runs to the end.
- `test_debug_endpoints.py` — `/step`/`/continue`/`/stop` owner-gated (non-owner 403, wrong-project
  cid 409, unknown cid 404); a debug fire → `node.paused` observed on the events stream → `step` to
  completion → `workflow.terminated`.

---

## 10. Non-goals (v1)

- Editing/injecting a payload at a breakpoint (inspect only).
- Back-stepping / time-travel.
- Per-branch control of concurrent fan-in messages (the queue is stepped in order; branches aren't
  individually pausable in v1).
- Persisting debug sessions across a farm restart (a restart ends in-flight runs; sessions are
  in-memory by design, like the hub).

---

Related: the Trace panel (observe half) — `js/trace-panel.js`, `admin.project_events`,
`graph_executor.on_trace`; multi-tenancy gating — `documents/multi_tenancy.md`; execution model —
`documents/technical_architecture.md`, `documents/runtime_dsl_specification.md`.
