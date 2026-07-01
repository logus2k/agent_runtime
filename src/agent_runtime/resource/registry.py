"""Resource registry — the declared set of manageable resources.

Adding a manageable thing = add a descriptor here (declare once). The generic Picker +
Manager render it with zero per-resource UI. Schemas for authored entities REUSE the Block
schema (single source of truth): an Agent resource IS the Agent block.
"""

from __future__ import annotations

from typing import Any

from .descriptor import (
    CREATE,
    DELETE,
    GET,
    LIST,
    PICK,
    UPDATE,
    ResourceDescriptor,
)


def _agent_schema() -> list[dict[str, Any]]:
    from ..composer.blocks import Agent
    return Agent().get_schema().to_catalog_entry()["config"]


def _trigger_schema() -> list[dict[str, Any]]:
    from ..composer.blocks import Trigger
    return Trigger().get_schema().to_catalog_entry()["config"]


def build_descriptors() -> list[ResourceDescriptor]:
    """The declared resources. Read-only catalogs = {LIST, PICK}; authored = full CRUD."""
    return [
        # --- authored entities (full CRUD) ---
        ResourceDescriptor(
            id="agent", label="Agent", icon="icons/robot.svg", identity="uid",
            source="runtime", capabilities={LIST, GET, CREATE, UPDATE, DELETE},
            schema=_agent_schema(), actions=["deploy", "import"],
            columns=["name", "persona", "enabled"],
        ),
        ResourceDescriptor(
            id="trigger", label="Trigger", icon="icons/clock-alarm-20.svg", identity="job_id",
            source="scheduler", capabilities={LIST, GET, CREATE, UPDATE, DELETE},
            schema=_trigger_schema(), actions=["pause", "resume", "run"],
            columns=["job_id", "trigger", "next_run_time"],
        ),
        ResourceDescriptor(
            id="recipe", label="Recipe", icon="icons/table.svg", identity="slug",
            source="client", capabilities={LIST, GET, CREATE, DELETE},
            schema=[{"key": "name", "control": "text", "label": "name"}],
            actions=["insert"], columns=["name"],
        ),
        # --- read-only catalogs (pick from reality) ---
        ResourceDescriptor(
            id="mcp-tool", label="MCP Tool", icon="icons/connectors.svg", identity="name",
            source="mcp", capabilities={LIST, PICK}, multi=True,
            schema=[
                {"key": "name", "control": "text", "label": "tool"},
                {"key": "description", "control": "textarea", "label": "description"},
            ],
            columns=["name", "description"],
        ),
        ResourceDescriptor(
            id="preset", label="Persona / Preset", icon="icons/robot.svg", identity="name",
            source="agent_server", capabilities={LIST, PICK},
            schema=[
                {"key": "name", "control": "text", "label": "preset"},
                {"key": "memory_policy", "control": "text", "label": "memory"},
            ],
            columns=["name", "memory_policy"],
        ),
        ResourceDescriptor(
            id="wa-target", label="WhatsApp target", icon="icons/whatsapp-icon.svg", identity="id",
            source="whatsapp", capabilities={LIST, PICK},
            schema=[
                {"key": "name", "control": "text", "label": "name"},
                {"key": "id", "control": "text", "label": "chat id"},
                {"key": "kind", "control": "text", "label": "kind"},
            ],
            columns=["name", "id", "kind"],
        ),
    ]


def descriptors_json() -> dict[str, Any]:
    return {"version": "1.0", "resources": [d.to_json() for d in build_descriptors()]}


def descriptor_by_id(rid: str) -> ResourceDescriptor | None:
    for d in build_descriptors():
        if d.id == rid:
            return d
    return None
