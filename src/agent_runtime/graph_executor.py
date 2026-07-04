"""GraphWorkflowExecutor — walk a persisted ``GraphRecord`` with fan-out / fan-in.

This is distinct from ``composer.executor.GraphExecutor`` (a single-path linear walker
that deliberately rejects fan-out): it executes the **persisted** ``GraphRecord`` shape
and implements the bus/event semantics the spec fixes for the graph form:

  * **Fan-out (§9.3.2)** — a node's output is delivered to **all** its successors; each
    successor edge is an independent message.
  * **Fan-in / per-message (§9.3.2)** — a node **runs once per incoming message**. There
    is **no barrier and no merge**: the block never waits for all sources, inputs are not
    synchronized. This falls out of a message-queue walk: each arrival is an independent
    unit of work, so a node with two incoming edges runs twice.

Node behaviour is injected as **handlers** (kind -> async callable) exactly like the
existing runner. A handler runs one node for one incoming message and returns the value
to hand to its successors. ``max_steps`` bounds the walk (a mis-wired cycle can never
spin forever — loud, never a silent hang).
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from .dsl_graph import GraphNode, GraphRecord

if TYPE_CHECKING:
    from .debug import DebugSession, PauseEmit

log = logging.getLogger("agent_runtime.graph_executor")

# A handler runs one node for one incoming message: (node, value, ctx) -> out_value.
NodeHandler = Callable[[GraphNode, Any, "WalkContext"], Awaitable[Any]]
# A trace hook, awaited once per edge crossed: (src, dst, port, value, ctx). ``value`` is the
# payload flowing on that edge (the source node's output) — for the live Trace panel.
TraceHook = Callable[[str, str, str, Any, "WalkContext"], Awaitable[None]]


@dataclass
class WalkContext:
    """Per-run context threaded through handlers (cid for tracing, a scratch bag)."""

    cid: str = "local"
    sid: int = 0
    sender: str = "agent_runtime.graph_executor"
    scratch: dict[str, Any] = field(default_factory=dict)


class GraphExecutionError(Exception):
    """A graph record could not be executed (unknown kind, step budget exceeded)."""


@dataclass
class _Msg:
    node_id: str
    value: Any


class GraphWorkflowExecutor:
    def __init__(
        self,
        handlers: dict[str, NodeHandler],
        *,
        max_steps: int = 1000,
        on_trace: Optional[TraceHook] = None,
        debug: Optional["DebugSession"] = None,
        on_pause: Optional["PauseEmit"] = None,
    ) -> None:
        self._handlers = dict(handlers)
        self._max_steps = max_steps
        self._on_trace = on_trace
        # Step-by-step debugging (documents/debug_specification.md). When ``debug`` is a
        # DebugSession, the walk pauses at ``session.gate(...)`` before running each node.
        # None (the default) → no gate is ever awaited: non-debug runs are unchanged.
        self._debug = debug
        self._on_pause = on_pause

    async def run(
        self, record: GraphRecord, initial: Any, ctx: Optional[WalkContext] = None,
        *, extra_seeds: Optional[list[str]] = None,
    ) -> WalkContext:
        """Execute the workflow record from its entry node. Returns the ``WalkContext``
        (its ``scratch`` accumulates run state — e.g. per-node run counts and the last
        value delivered to each sink). Raises ``GraphExecutionError`` on an unknown node
        kind or if the step budget is exceeded.

        ``extra_seeds`` are additional node ids to seed the walk with (value ``None``) besides
        the entry — used for pure runtime flow SOURCES with no incoming edge (e.g. a Data/JSON
        block feeding a normal successor). Their handler still runs and fans out normally."""
        ctx = ctx or WalkContext()
        run_counts: dict[str, int] = ctx.scratch.setdefault("run_counts", {})
        sink_values: dict[str, Any] = ctx.scratch.setdefault("sink_values", {})

        queue: deque[_Msg] = deque([_Msg(record.entry, initial)])
        for seed in extra_seeds or []:
            if seed != record.entry:
                queue.append(_Msg(seed, None))
        steps = 0

        while queue:
            steps += 1
            if steps > self._max_steps:
                raise GraphExecutionError(
                    f"step budget {self._max_steps} exceeded (graph {record.uid}) — "
                    f"likely a mis-wired cycle (no exit)"
                )
            msg = queue.popleft()
            node = record.node(msg.node_id)
            handler = self._handlers.get(node.kind)
            if handler is None:
                raise GraphExecutionError(
                    f"no handler for node kind '{node.kind}' (node '{node.id}'); "
                    f"known: {sorted(self._handlers)}"
                )

            # Debug pause-gate: BEFORE running the node, block until the user steps/continues
            # (or raise DebugStopped on stop). Inert when not debugging (self._debug is None).
            if self._debug is not None:
                await self._debug.gate(node.id, node.kind, msg.value, self._on_pause)

            # Run this node ONCE for THIS incoming message (per-message fan-in: no
            # barrier — a node with two incoming edges is visited twice and runs twice).
            out_value = await handler(node, msg.value, ctx)
            run_counts[node.id] = run_counts.get(node.id, 0) + 1

            # A `vars` edge is a PULL input (the destination reads it from ctx.scratch when it
            # runs), NOT a triggering message — exclude it from fan-out so it never enqueues a
            # run of the destination. It is also not what makes a node a "sink".
            successors = [e for e in record.out_edges(node.id) if e.dst_port != "vars"]
            if not successors:
                # A sink (destination / terminal): record its value, no fan-out.
                sink_values[node.id] = out_value
                continue

            # Fan-out: deliver the output to EVERY successor as an independent message.
            for edge in successors:
                if self._on_trace is not None:
                    await self._on_trace(edge.src, edge.dst, edge.port, out_value, ctx)
                queue.append(_Msg(edge.dst, out_value))

        return ctx
