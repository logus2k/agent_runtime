"""composer — the authoritative Block/Edge model + lowering for declarative agents.

This package is the single source of truth that replaces the hand-synced pair
``patron/js/compile.js`` ↔ ``agent_runtime/dsl.py``. It lives WITH the executor on
purpose (António, 2026-06-30): Patron is a *client* (one of possibly several), so the
contract cannot live in any client — clients fetch it via the catalog.

See ``patron/documents/composer_design_and_plan.md`` for the full design. Layering:

    DataSchema → Port / ConfigField → BlockSchema   (the contract a block describes)
    Block (Manageable) → Agent | Activity | Destination   (the three families, for now)
    Edge   = the agent_bus envelope header (cid/sid/sender/UTC-ts) + a typed payload
    Catalog = the block-schema catalog the editor renders from
    Graph.lower() = serialized graph → runtime DSL (link-traced, not presence-based)
"""

from __future__ import annotations

from .blocks import (
    Activity,
    Agent,
    Block,
    Branch,
    Bus,
    Composite,
    Destination,
    Loop,
    Manageable,
    TTS,
    Transform,
    Trigger,
    WhatsApp,
)
from .catalog import BLOCK_TYPES, Catalog
from .edge import Edge, TraceRecord
from .executor import (
    ExecContext,
    ExecutionError,
    GraphExecutor,
    StepResult,
    composite_handler,
)
from .ir import IREdge, IRGraph, IRNode
from .lower import Graph, LoweringError, lower_graph
from .schema import BlockSchema, ConfigField, DataSchema, Port

__all__ = [
    "DataSchema",
    "Port",
    "ConfigField",
    "BlockSchema",
    "Edge",
    "TraceRecord",
    "Manageable",
    "Block",
    "Agent",
    "Activity",
    "Destination",
    "Trigger",
    "Transform",
    "Branch",
    "Loop",
    "Composite",
    "WhatsApp",
    "TTS",
    "Bus",
    "Catalog",
    "BLOCK_TYPES",
    "Graph",
    "lower_graph",
    "LoweringError",
    "IRNode",
    "IREdge",
    "IRGraph",
    "GraphExecutor",
    "StepResult",
    "ExecContext",
    "ExecutionError",
    "composite_handler",
]
