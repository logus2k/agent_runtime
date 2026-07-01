"""Catalog — the editor's data source, built from the registered Block classes.

``Catalog.entries()`` is what ``GET /composer/catalog`` returns: one
``BlockSchema.to_catalog_entry()`` per registered block type. The editor renders the
palette, port stubs, edge-compatibility, and property forms entirely from this — so
adding a block is a Python change only (no JS edit), and there is exactly ONE
authoritative contract (this package), never a JS copy.

Graphs are authored in this vocabulary directly (node ``type`` == ``Block.kind``), so
``lower.py`` instantiates blocks straight from ``BLOCK_TYPES`` — there is no legacy
node-type adapter.
"""

from __future__ import annotations

from typing import Any

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
