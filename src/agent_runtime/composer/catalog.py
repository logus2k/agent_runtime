"""Catalog — the editor's data source, built from the registered Block classes.

``Catalog.entries()`` is what ``GET /composer/catalog`` returns: one
``BlockSchema.to_catalog_entry()`` per registered block type. The editor renders the
palette, port stubs, edge-compatibility, and property forms entirely from this — so
adding a block is a Python change only (no JS edit), and there is exactly ONE
authoritative contract (this package), never a JS copy.

``block_for_graph_type`` bridges the *current* serialized-graph node-type ids (the
``patron/agent/*`` vocabulary the existing fixture uses) to block constructors, so the
link-traced lowering in ``lower.py`` can fold today's graphs into the new model and
reproduce today's output. As the editor migrates to the new vocabulary (Phase 2), new
type ids map straight to these classes.
"""

from __future__ import annotations

from typing import Any, Callable

from .blocks import (
    Agent,
    Block,
    Branch,
    Bus,
    Composite,
    Loop,
    TTS,
    Transform,
    Trigger,
    WhatsApp,
)

# The block types the catalog advertises. Keyed by the new-vocabulary type id
# (== Block.kind). Branch/Loop (Control) un-deferred in Phase 3; Composite in Phase 4.
BLOCK_TYPES: dict[str, type[Block]] = {
    Agent.kind: Agent,
    Trigger.kind: Trigger,
    Transform.kind: Transform,
    Branch.kind: Branch,
    Loop.kind: Loop,
    Composite.kind: Composite,
    WhatsApp.kind: WhatsApp,
    TTS.kind: TTS,
    Bus.kind: Bus,
}


class Catalog:
    """Serializes the registered block types into the editor catalog."""

    def __init__(self, types: dict[str, type[Block]] | None = None) -> None:
        self._types = dict(types if types is not None else BLOCK_TYPES)

    def entries(self) -> list[dict[str, Any]]:
        """One catalog entry per registered block type (stable order by type id)."""
        out: list[dict[str, Any]] = []
        for kind in sorted(self._types):
            block = self._types[kind]()
            out.append(block.get_schema().to_catalog_entry())
        return out

    def to_json(self) -> dict[str, Any]:
        return {"version": "1.0", "blocks": self.entries()}


# --- bridge: current serialized-graph node types -> block constructors ----------
# Each entry knows how to read a litegraph node's ``properties`` and produce a
# configured Block. Capability nodes (tools/rag/guardrail) don't become their own
# block — they fold into the Agent's config, which is exactly the new model (tools &c
# are config on the Agent, not separate nodes). lower.py performs that folding.
_GRAPH_TYPE_BUILDERS: dict[str, Callable[[dict[str, Any]], Block]] = {
    "patron/agent/trigger": lambda p: Trigger(
        uid="trigger",
        config={
            "agent_id": p.get("agent_id", "untitled-agent"),
            "trigger_type": p.get("trigger_type", "schedule"),
            "cron": p.get("cron"),
            "timezone": p.get("timezone"),
        },
    ),
    "patron/dest/whatsapp": lambda p: WhatsApp(uid="whatsapp", config={"target": p.get("target", "")}),
    "patron/dest/tts": lambda p: TTS(uid="tts", config={"target": p.get("target", "")}),
    "patron/dest/bus": lambda p: Bus(uid="bus", config={"target": p.get("target", "")}),
}


def block_for_graph_type(node_type: str, properties: dict[str, Any]) -> Block | None:
    """Build the Block for a non-Agent graph node (Trigger/Destination), or None if
    this node type folds into the Agent (brain/tools/rag/guardrail/deliver) or is
    unknown. lower.py handles the Agent assembly separately."""
    builder = _GRAPH_TYPE_BUILDERS.get(node_type)
    return builder(properties) if builder else None
