"""The value objects a block's contract is made of.

``DataSchema`` is the structural shape a port carries — a **JSON-Schema subset**
(decided 2026-06-30). It is *not* a type-name string: edge validation, codegen, and
composition all key off the structure, so two ports are compatible when their shapes
are structurally compatible, not when their names happen to match.

These types are pure data + small pure methods — no I/O, no framework — so they are
trivial to test and to serialize into the editor's catalog.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

Direction = Literal["in", "out"]


@dataclass(frozen=True)
class DataSchema:
    """A port payload's structural shape — a JSON-Schema subset.

    Supported today: ``type`` (``string``/``number``/``integer``/``boolean``/
    ``object``/``any``) and, for objects, a ``properties`` map of name -> DataSchema
    plus a ``required`` set. That covers the current str→str flow and the structured
    payloads a real Transform will bridge; richer JSON-Schema lands when a block needs
    it (we deliberately start small rather than vendor a full validator).
    """

    type: str = "any"
    properties: dict[str, "DataSchema"] = field(default_factory=dict)
    required: frozenset[str] = frozenset()

    def is_compatible_with(self, consumer: "DataSchema") -> bool:
        """Can a value of *this* (a producer/``out`` port) feed *consumer* (an ``in``
        port)? Structural sub-typing: ``any`` accepts/produces anything; objects are
        compatible when the producer supplies every property the consumer *requires*,
        each recursively compatible. Mismatched scalar types are incompatible — caught
        at edge-draw time, not at runtime.
        """
        if consumer.type == "any" or self.type == "any":
            return True
        if self.type != consumer.type:
            return False
        if consumer.type == "object":
            for name in consumer.required:
                prod = self.properties.get(name)
                if prod is None:
                    return False
                if not prod.is_compatible_with(consumer.properties[name]):
                    return False
        return True

    def to_json(self) -> dict[str, Any]:
        """Serialize to a plain JSON-Schema-ish dict (for the catalog)."""
        out: dict[str, Any] = {"type": self.type}
        if self.properties:
            out["properties"] = {k: v.to_json() for k, v in self.properties.items()}
        if self.required:
            out["required"] = sorted(self.required)
        return out

    @classmethod
    def from_json(cls, d: Any) -> "DataSchema":
        """Parse the dict form. A bare missing/empty value is the permissive ``any``."""
        if not d:
            return cls(type="any")
        if not isinstance(d, dict):
            raise ValueError(f"DataSchema must be a JSON object, got {type(d).__name__}")
        props = {k: cls.from_json(v) for k, v in (d.get("properties") or {}).items()}
        return cls(
            type=str(d.get("type", "any")),
            properties=props,
            required=frozenset(d.get("required") or ()),
        )


# Convenience singletons for the common shapes.
STRING = DataSchema(type="string")
ANY = DataSchema(type="any")


@dataclass(frozen=True)
class Port:
    """One typed connection point on a block."""

    name: str
    direction: Direction
    schema: DataSchema = ANY

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "direction": self.direction, "schema": self.schema.to_json()}


@dataclass(frozen=True)
class ConfigField:
    """One non-wired setting on a block (rendered as a property in the editor).

    ``kind`` is a UI/validation hint, not a data type: ``preset-ref`` and
    ``mcp-tool-refs`` are *grounded pickers* (the editor offers real presets / MCP
    tools), which is what makes capabilities config rather than wired ports.
    """

    key: str
    kind: str = "string"  # string | number | integer | boolean | enum | preset-ref | mcp-tool-refs | sampling-overrides | json
    required: bool = False
    values: Optional[list[Any]] = None  # for kind == "enum"
    default: Any = None
    # --- input-rendering metadata (how the editor should render this field) ---
    # This is the block's own declaration of how its inputs are edited: the editor's
    # Properties panel renders each field by its ``control`` (not a hardcoded guess).
    control: str = "text"          # text | textarea | json | number | select
    label: Optional[str] = None    # friendly caption (defaults to key)
    placeholder: Optional[str] = None
    min: Optional[float] = None    # for control == "number"
    max: Optional[float] = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key, "kind": self.kind, "required": self.required,
            "control": self.control, "label": self.label or self.key,
        }
        if self.values is not None:
            out["values"] = self.values
        if self.default is not None:
            out["default"] = self.default
        if self.placeholder is not None:
            out["placeholder"] = self.placeholder
        if self.min is not None:
            out["min"] = self.min
        if self.max is not None:
            out["max"] = self.max
        return out


@dataclass(frozen=True)
class BlockSchema:
    """Exactly what ``Block.get_schema()`` returns — the block's full contract.

    One source of truth, four consumers: render (catalog) · validate edges · codegen a
    Transform · lower to IR.
    """

    kind: str          # the block type id, e.g. "agent"
    category: str      # the family: Activity | Destination | Agent
    label: str
    ports: list[Port] = field(default_factory=list)
    config: list[ConfigField] = field(default_factory=list)

    def port(self, direction: Direction) -> Optional[Port]:
        """The single flow port in a direction (the model uses one in / one out)."""
        for p in self.ports:
            if p.direction == direction:
                return p
        return None

    def config_field(self, key: str) -> Optional[ConfigField]:
        for c in self.config:
            if c.key == key:
                return c
        return None

    def to_catalog_entry(self) -> dict[str, Any]:
        """Serialize for ``GET /composer/catalog`` — the editor's data source."""
        ports: dict[str, Any] = {}
        for p in self.ports:
            ports[p.direction] = {"name": p.name, "schema": p.schema.to_json()}
        return {
            "type": self.kind,
            "category": self.category,
            "label": self.label,
            "ports": ports,
            "config": [c.to_json() for c in self.config],
        }
