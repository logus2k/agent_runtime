"""The graph-form IR — the runtime representation of a NON-linear composer graph.

The flat ``AgentRecord`` (``dsl.py``) is the degenerate *linear* graph. The IR here is
the general form: a set of typed nodes and directed, port-labelled edges with an entry
node. It is what the ``GraphExecutor`` walks so branching/looping are executed by
following real edges — the graph is load-bearing at run time, not just at compile time
(runtime_dsl_specification.md §6).

Deliberately minimal + pure data (no execution logic here). ``port`` on an edge is the
source out-port label: ``"out"`` for a plain node, or a branch label (e.g. ``"then"`` /
``"else"``) for a Branch, or ``"body"`` / ``"exit"`` for a Loop. This is a Phase-3
proposal — the exact port vocabulary is open (design §7 Phase 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class IRNode:
    id: str
    kind: str                       # agent | action | branch | loop | trigger | destination | ...
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IREdge:
    src: str                        # source node id
    dst: str                        # destination node id
    port: str = "out"               # source out-port label (branch/loop routing)


@dataclass
class IRGraph:
    nodes: dict[str, IRNode]
    edges: list[IREdge]
    entry: str

    def __post_init__(self) -> None:
        if self.entry not in self.nodes:
            raise ValueError(f"IRGraph entry '{self.entry}' is not a node")
        for e in self.edges:
            if e.src not in self.nodes:
                raise ValueError(f"IREdge source '{e.src}' is not a node")
            if e.dst not in self.nodes:
                raise ValueError(f"IREdge destination '{e.dst}' is not a node")

    def out_edges(self, node_id: str, port: Optional[str] = None) -> list[IREdge]:
        """Outgoing edges from a node, optionally filtered to one out-port label."""
        return [
            e for e in self.edges
            if e.src == node_id and (port is None or e.port == port)
        ]

    def is_sink(self, node_id: str) -> bool:
        """A node with no outgoing edges — a terminal (destination) of the walk."""
        return not any(e.src == node_id for e in self.edges)

    def node(self, node_id: str) -> IRNode:
        return self.nodes[node_id]
