"""GraphExecutor — walk an ``IRGraph``, routing through branches and loops.

This is the proof that the graph is load-bearing at EXECUTION time: the next node is
whatever the current node's chosen out-edge points to, not a hardcoded stage order.

Node behaviour is injected as **handlers** (kind -> async callable), exactly like the
existing runner uses fakes/real services. A handler returns either:

  * a plain value                     -> follow the single ``"out"`` edge, or
  * ``StepResult(value, port=...)``   -> follow the edge on that out-port (Branch/Loop).

The executor itself only does routing + safety (a ``max_steps`` guard so a mis-wired
loop can never spin forever — no silent hang). It is service-agnostic, so it is fully
unit-testable with fake handlers; wiring real Agent/Action handlers (agent_server + MCP)
is a thin adapter on top (Phase 3 integration, kept out of the pure core).

Management interface (design §3.4): before running a node, the executor calls the
optional ``authorize`` hook (security) and, per edge crossed, emits a trace via the
optional ``on_trace`` hook (traceability) — so every traversal is governable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .ir import IRGraph, IRNode

log = logging.getLogger("agent_runtime.composer.executor")


@dataclass
class StepResult:
    """A handler's result when it needs to pick an out-port (Branch/Loop)."""

    value: Any
    port: str = "out"


# A handler runs one node: (node, incoming_value, ctx) -> value | StepResult.
Handler = Callable[[IRNode, Any, "ExecContext"], Awaitable[Any]]


@dataclass
class ExecContext:
    """Per-run context threaded through handlers (cid for tracing, a scratch bag)."""

    cid: str = "local"
    sid: int = 0
    sender: str = "composer.executor"
    scratch: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.scratch is None:
            self.scratch = {}


class ExecutionError(Exception):
    """A graph could not be executed (no route, unknown kind, step budget exceeded)."""


class GraphExecutor:
    def __init__(
        self,
        handlers: dict[str, Handler],
        *,
        max_steps: int = 1000,
        authorize: Optional[Callable[[IRNode, Any, ExecContext], None]] = None,
        on_trace: Optional[Callable[[str, str, str, ExecContext], None]] = None,
    ) -> None:
        self._handlers = dict(handlers)
        self._max_steps = max_steps
        self._authorize = authorize
        self._on_trace = on_trace

    async def run(self, graph: IRGraph, initial: Any, ctx: Optional[ExecContext] = None) -> Any:
        """Execute the graph from its entry node; return the value at the sink reached.

        Raises ``ExecutionError`` on a dead end, an unknown node kind, or if the step
        budget is exceeded (a mis-wired cycle) — loudly, never a silent hang."""
        ctx = ctx or ExecContext()
        current = graph.entry
        value = initial
        steps = 0

        while True:
            steps += 1
            if steps > self._max_steps:
                raise ExecutionError(
                    f"step budget {self._max_steps} exceeded at node '{current}' — "
                    f"likely a mis-wired loop (no exit)"
                )
            node = graph.node(current)
            handler = self._handlers.get(node.kind)
            if handler is None:
                raise ExecutionError(
                    f"no handler for node kind '{node.kind}' (node '{node.id}'); "
                    f"known: {sorted(self._handlers)}"
                )
            if self._authorize is not None:
                self._authorize(node, value, ctx)  # security gate; raises to deny

            result = await handler(node, value, ctx)
            if isinstance(result, StepResult):
                value, port = result.value, result.port
            else:
                value, port = result, "out"

            if graph.is_sink(current):
                return value  # terminal reached

            edges = graph.out_edges(current, port)
            if not edges:
                raise ExecutionError(
                    f"node '{current}' returned port '{port}' but has no matching "
                    f"out-edge (ports available: "
                    f"{sorted({e.port for e in graph.out_edges(current)})})"
                )
            if len(edges) > 1:
                raise ExecutionError(
                    f"node '{current}' has {len(edges)} edges on port '{port}'; "
                    f"a single route is required (fan-out is a parallel construct, "
                    f"not yet modelled)"
                )
            nxt = edges[0]
            if self._on_trace is not None:
                self._on_trace(nxt.src, nxt.dst, nxt.port, ctx)
            current = nxt.dst


def composite_handler(
    sub_handlers: dict[str, Handler],
    *,
    max_steps: int = 1000,
    authorize: Optional[Callable[[IRNode, Any, ExecContext], None]] = None,
    on_trace: Optional[Callable[[str, str, str, ExecContext], None]] = None,
) -> Handler:
    """A handler for ``kind == "composite"`` that runs the node's inner IRGraph in a
    nested executor — this is how nesting (Phase 4) executes: a workflow-as-a-block is
    just a node whose behaviour is "run my inside". The inner graph comes from
    ``node.config["inner"]`` (an IRGraph). Nesting composes to any depth because the
    inner graph may itself contain composite nodes (shared ``sub_handlers``).
    """

    async def handler(node: IRNode, value: Any, ctx: ExecContext) -> Any:
        inner = node.config.get("inner")
        if inner is None:
            raise ExecutionError(
                f"composite node '{node.id}' has no inner graph (config['inner'])"
            )
        sub = GraphExecutor(
            sub_handlers, max_steps=max_steps, authorize=authorize, on_trace=on_trace
        )
        return await sub.run(inner, value, ctx)

    return handler
