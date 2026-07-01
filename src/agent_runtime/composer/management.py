"""Management hardening — the universal NFR contract, enforced during execution.

The Block Management interface (traceability, debug, security — design §3.4) is only
real if the runtime ENFORCES it. This module turns it into executor hooks:

* **Traceability** — ``TraceCollector`` produces an ``on_trace`` callback that, for every
  edge the executor crosses, stamps a real agent_bus envelope (via ``new_event`` → a UTC
  ISO-8601 timestamp) and records the ``TraceRecord`` (cid/sid/sender/UTC-ts + src→dst).
  So every traversal is traceable, keyed by cid, using the SAME envelope the bus uses.
* **Security** — ``make_authorizer`` produces an ``authorize`` callback the executor
  calls before running each node; a denied node raises ``AuthorizationError`` and its
  handler never runs (fail-closed, loud).

Both compose onto ``GraphExecutor(authorize=..., on_trace=...)``. A per-block override
still lives on ``Block.authorize`` (default allow); this is the graph-level policy that
wires those obligations into the run.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from agent_bus_client import new_event

from .edge import TraceRecord
from .executor import ExecContext
from .ir import IRNode


class AuthorizationError(Exception):
    """A node was denied by policy (security gate). Fail-closed and loud."""


class TraceCollector:
    """Collects a ``TraceRecord`` per edge crossed, each stamped with a real UTC
    timestamp from the agent_bus envelope path."""

    def __init__(self, *, stream_id: str = "agent-runtime", event_type: str = "edge.traversed") -> None:
        self.records: list[TraceRecord] = []
        self._stream_id = stream_id
        self._event_type = event_type

    def on_trace(self, src: str, dst: str, port: str, ctx: ExecContext) -> None:
        env = new_event(
            stream_id=self._stream_id,
            cid=ctx.cid,
            sid=ctx.sid,
            sender=ctx.sender,
            event_type=self._event_type,
            data={"src": src, "dst": dst, "port": port},
        )
        self.records.append(
            TraceRecord(
                edge=f"{src}.{port} -> {dst}",
                cid=env.header.cid,
                sid=env.header.sid,
                sender=env.header.sender,
                timestamp=env.header.timestamp,  # UTC ISO-8601
                event_type=env.header.event_type,
            )
        )


def make_authorizer(
    *,
    deny_kinds: Optional[Iterable[str]] = None,
    predicate: Optional[Callable[[IRNode, Any, ExecContext], bool]] = None,
) -> Callable[[IRNode, Any, ExecContext], None]:
    """Build an ``authorize`` hook. Denies a node when its kind is in ``deny_kinds`` or
    when ``predicate(node, value, ctx)`` returns False. Raises ``AuthorizationError``
    (fail-closed) so the executor stops before the node runs."""
    denied = set(deny_kinds or ())

    def authorize(node: IRNode, value: Any, ctx: ExecContext) -> None:
        if node.kind in denied:
            raise AuthorizationError(
                f"node '{node.id}' (kind '{node.kind}') denied by policy"
            )
        if predicate is not None and not predicate(node, value, ctx):
            raise AuthorizationError(
                f"node '{node.id}' (kind '{node.kind}') denied by predicate"
            )

    return authorize
