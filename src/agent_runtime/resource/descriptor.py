"""Resource descriptor — the management-plane twin of a Block's ``get_schema()``.

A Resource is to management what a Block is to authoring: self-describing metadata that
the editor renders with ONE generic Picker + ONE generic Manager, never a bespoke panel.
See ``documents/resource_model.md``.

A descriptor declares WHAT a resource is (id, label, icon, identity, schema) and WHAT you
can do with it (capabilities + extra action verbs). WHERE it comes from is the ``source``
key — a hint naming the backing service/SDK; the concrete ``list``/``get``/… live in
``sources.py`` (server-reachable) or in the client (patron-local resources like recipes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Capability verbs. Read-only catalogs declare {LIST, PICK}; authored entities add CRUD.
LIST = "list"
GET = "get"
PICK = "pick"
CREATE = "create"
UPDATE = "update"
DELETE = "delete"


@dataclass
class ResourceDescriptor:
    id: str                         # "agent" | "trigger" | "mcp-tool" | "preset" | "wa-target" | "recipe"
    label: str                      # "Agent", "Trigger", …
    icon: str                       # icons/*.svg (reuse the block-icon convention)
    identity: str                   # the key field ("uid", "job_id", "name", "id", "slug")
    source: str                     # backing SDK/service: "runtime"|"scheduler"|"mcp"|"agent_server"|"whatsapp"|"client"
    capabilities: set[str]          # {LIST, PICK, CREATE, UPDATE, DELETE, GET}
    # The fields (as composer ConfigField dicts — the SAME renderer as the Properties panel).
    # For authored entities this is the Block's config; for catalogs it's the display columns.
    schema: list[dict[str, Any]] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)  # extra verbs: deploy/import/pause/resume/run/insert
    # Optional display hints for the generic list view (which schema keys to show as columns).
    columns: list[str] = field(default_factory=list)
    multi: bool = False             # a picker for this resource selects MANY (e.g. mcp-tool allow-list)
    editable: bool = False          # the Manager offers a schema-driven edit form (else create/edit is the composer's job)
    # Picker refinements (keep the generic picker able to replace bespoke ones):
    group_by: Optional[str] = None  # optgroup the single-select by this item field (e.g. "kind")
    allow_free: bool = False        # allow typing an id not in the list ("type an id…")
    # When an item is picked, also set sibling node fields from item fields: {siblingKey: itemField}
    # e.g. {"target_name": "name"} — picking a WhatsApp target fills the friendly name.
    sets: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "icon": self.icon,
            "identity": self.identity,
            "source": self.source,
            "capabilities": sorted(self.capabilities),
            "schema": self.schema,
            "actions": self.actions,
            "columns": self.columns or [self.identity],
            "multi": self.multi,
            "editable": self.editable,
            "group_by": self.group_by,
            "allow_free": self.allow_free,
            "sets": self.sets,
        }
