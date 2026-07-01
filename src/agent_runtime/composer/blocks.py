"""The Block model: the base contract + the three families (Activity, Destination,
Agent) and their concrete leaves.

Design (``patron/documents/composer_design_and_plan.md`` §3–§4):

* A **Block** is self-describing via ``get_schema()`` (its typed ports + config). That
  one description has four consumers: render, validate edges, codegen a Transform,
  lower to IR.
* Every Block carries **two interfaces**:
  - *Functional* = ``get_schema()`` + ``lower()`` — varies per type (abstract here).
  - *Management* = ``Manageable`` (traceability, debug, security) — UNIVERSAL, given
    concrete defaults on the base so every leaf is pluggable AND governable.
* A leaf author writes only ``get_schema()`` + ``lower()``; identity, config,
  ``validate()``, catalog emission, and the whole Management interface come free.

``lower()`` returns this block's **fragment** of the flat runtime DSL (the
``AgentRecord`` shape in ``dsl.py``). ``Graph.lower()`` (see ``lower.py``) merges the
fragments in flow order. The flat record is the degenerate linear graph; Branch/Loop/
Composite (the graph form) are deferred (Phases 3–4).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Optional

from .schema import ANY, STRING, BlockSchema, ConfigField, DataSchema, Port


# --------------------------------------------------------------------------- #
# Management interface — the universal NFR contract every Block must satisfy.
# --------------------------------------------------------------------------- #
class Manageable(ABC):
    """Traceability, debug, security — the contract that makes a block governable.

    Declared abstract so the obligation is explicit; ``Block`` supplies safe concrete
    defaults so leaves inherit it for free and override only to specialize.
    """

    @abstractmethod
    def authorize(self, envelope: Any) -> None:
        """Security gate: raise to deny a message; return to allow."""

    @abstractmethod
    def trace_record(self, edge: Any, envelope: Any) -> Any:
        """Traceability: project an edge traversal into a trace record."""

    @abstractmethod
    def inspect(self) -> dict[str, Any]:
        """Debug: a JSON-able snapshot of this block's identity + config."""


# --------------------------------------------------------------------------- #
# Block base.
# --------------------------------------------------------------------------- #
class Block(Manageable, ABC):
    """Base of every participant. Identity + config + the two interfaces.

    Subclasses set the class attributes ``kind``/``category``/``label`` and implement
    ``get_schema()`` and ``lower()``. Everything else is provided here.
    """

    kind: str = "block"
    category: str = "Block"
    label: str = "Block"

    def __init__(self, *, uid: Optional[str] = None, config: Optional[dict[str, Any]] = None) -> None:
        self.uid = uid or self.kind
        self._config: dict[str, Any] = dict(config or {})

    # ---- Functional: abstract (each block type implements) ----
    @abstractmethod
    def get_schema(self) -> BlockSchema:
        """The block's typed ports + config — the single source of truth."""

    @abstractmethod
    def lower(self) -> dict[str, Any]:
        """This block's fragment of the flat runtime DSL (merged by Graph.lower())."""

    # ---- Functional: concrete (derived from config + schema) ----
    def get_config(self) -> dict[str, Any]:
        return dict(self._config)

    def set_config(self, config: dict[str, Any]) -> None:
        self._config = dict(config)

    def cfg(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def ports(self, direction: str) -> list[Port]:
        return [p for p in self.get_schema().ports if p.direction == direction]

    def validate(self) -> list[str]:
        """Default: every ``required`` config field must be present and non-empty.

        Leaves extend (call ``super().validate()`` then add type/shape rules). Errors
        are human-aimed strings; empty list == valid.
        """
        errors: list[str] = []
        schema = self.get_schema()
        for f in schema.config:
            if f.required:
                v = self._config.get(f.key)
                if v is None or (isinstance(v, str) and not v.strip()):
                    errors.append(f"{self.label}: required config '{f.key}' is missing/empty")
            if f.kind == "enum" and f.values is not None and f.key in self._config:
                v = self._config[f.key]
                if v not in f.values:
                    errors.append(
                        f"{self.label}: config '{f.key}'={v!r} must be one of {f.values}"
                    )
        return errors

    # ---- Management: concrete universal defaults (override to specialize) ----
    def authorize(self, envelope: Any) -> None:  # noqa: D401 - default allow
        """Default policy: allow. Override on a block that must restrict senders."""
        return None

    def trace_record(self, edge: Any, envelope: Any) -> Any:
        """Default: delegate to the Edge's own projection (uniform trace shape)."""
        return edge.trace(envelope)

    def inspect(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "kind": self.kind,
            "category": self.category,
            "label": self.label,
            "config": self.get_config(),
        }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<{type(self).__name__} uid={self.uid!r} config={self._config!r}>"


# --------------------------------------------------------------------------- #
# Helpers shared by leaves (mirror compile.js's csv()/JSON-vars handling so the
# lowering is byte-identical to today's output — see tests/test_composer_lower.py).
# --------------------------------------------------------------------------- #
def _csv(value: Any) -> list[str]:
    """'a, b ,' -> ['a','b']  (split on comma, trim, drop empties) — like compile.js."""
    return [x.strip() for x in str(value or "").split(",") if x.strip()]


def _json_obj(value: Any, *, where: str) -> dict[str, Any]:
    """Parse a JSON-object string (Brain.input_vars). Loud on malformed input."""
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"{where} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{where} must be a JSON object, got {type(parsed).__name__}")
    return parsed


# --------------------------------------------------------------------------- #
# Family: Agent (its own family — the workhorse — and composable).
# --------------------------------------------------------------------------- #
class Agent(Block):
    """The workhorse. ``in: str -> out: str``. Capabilities (persona/model, tools,
    memory, rag, guardrails) are CONFIG, not ports. The LLM *model* is NOT a field:
    the ``persona`` (an agent_server preset) selects it on agent_server.

    Composable: an Agent is itself susceptible of composition — a participant in a
    workflow whose inside can later be a graph of participants (nesting, §1).

    Capability config the lowering reads (folded from the graph by ``lower.py``):
      persona, temperature, max_tokens, input_template, input_vars,
      tools_server, tools_allow, tools_max_rounds,
      rag_rewriter, rag_domains, rag_use_graph,
      guard_forbidden, guard_min_confidence.
    """

    kind = "agent"
    category = "Agent"
    label = "Agent"

    def get_schema(self) -> BlockSchema:
        return BlockSchema(
            kind=self.kind,
            category=self.category,
            label=self.label,
            ports=[Port("in", "in", STRING), Port("out", "out", STRING)],
            config=[
                ConfigField("persona", "preset-ref", required=True),  # selects the model on agent_server
                ConfigField("llm", "sampling-overrides"),             # temperature/max_tokens/… — NOT model
                ConfigField("tools", "mcp-tool-refs"),
                ConfigField("memory", "enum", values=["none", "thread_window"], default="none"),
            ],
        )

    def validate(self) -> list[str]:
        errors = super().validate()
        # input_vars must be a JSON object when present (caught here, not at runtime).
        if self.cfg("input_vars") not in (None, ""):
            try:
                _json_obj(self.cfg("input_vars"), where=f"{self.label} input_vars")
            except ValueError as exc:
                errors.append(str(exc))
        return errors

    def lower(self) -> dict[str, Any]:
        frag: dict[str, Any] = {
            "brain": {
                "persona": self.cfg("persona", ""),
                "llm": {
                    "temperature": float(self.cfg("temperature", 0.3)),
                    "max_tokens": int(self.cfg("max_tokens", 1024)),
                },
            }
        }
        # Optional capabilities — emitted only when the graph attached them (config
        # refs, not ports). Order mirrors compile.js: rag, tools, guardrails, input.
        if self.cfg("rag_present"):
            frag["rag"] = {
                "rewriter": self.cfg("rag_rewriter") or None,
                "domains": _csv(self.cfg("rag_domains")),
                "use_graph": bool(self.cfg("rag_use_graph")),
            }
        if self.cfg("tools_present"):
            frag["tools"] = {
                "server": self.cfg("tools_server", ""),
                "allow": _csv(self.cfg("tools_allow")),
                "max_rounds": int(self.cfg("tools_max_rounds", 3)),
            }
        if self.cfg("guard_present"):
            frag["guardrails"] = {
                "forbidden": _csv(self.cfg("guard_forbidden")),
                "min_confidence": float(self.cfg("guard_min_confidence", 0.5)),
            }
        frag["input"] = {
            "template": self.cfg("input_template", "") or "",
            "vars": _json_obj(self.cfg("input_vars"), where=f"{self.label} input_vars"),
        }
        return frag


# --------------------------------------------------------------------------- #
# Family: Activity (deterministic work / boundary; in + out).
# --------------------------------------------------------------------------- #
class Activity(Block):
    """Base for deterministic work / boundary blocks. Has an in and an out flow port
    by default; ``Trigger`` overrides to be boundary (out only)."""

    category = "Activity"

    def get_schema(self) -> BlockSchema:  # generic shape; leaves override
        return BlockSchema(
            kind=self.kind,
            category=self.category,
            label=self.label,
            ports=[Port("in", "in", ANY), Port("out", "out", ANY)],
            config=[],
        )

    def lower(self) -> dict[str, Any]:
        return {}


class Trigger(Activity):
    """Boundary source: fires the agent. ``out`` only. Carries the agent id + the
    schedule (cron/timezone) — the *when* lives beside the record as a scheduler job,
    not inside it (compile.js does the same)."""

    kind = "trigger"
    label = "Trigger"

    def get_schema(self) -> BlockSchema:
        return BlockSchema(
            kind=self.kind,
            category=self.category,
            label=self.label,
            ports=[Port("out", "out", ANY)],
            config=[
                ConfigField("agent_id", "string", required=True),
                ConfigField("trigger_type", "enum", values=["schedule", "channel"], default="schedule"),
                ConfigField("cron", "string", default="0 7 * * *"),
                ConfigField("timezone", "string"),
            ],
        )

    def lower(self) -> dict[str, Any]:
        return {
            "id": self.cfg("agent_id", "untitled-agent"),
            "trigger": {"type": self.cfg("trigger_type", "schedule") or "schedule"},
        }

    def schedule_spec(self) -> Optional[dict[str, Any]]:
        """The scheduler-job side (cron + timezone), or None for a non-schedule
        trigger. Mirrors compile.js: cron defaults to '0 7 * * *', tz '' means UTC."""
        if (self.cfg("trigger_type", "schedule") or "schedule") != "schedule":
            return None
        return {
            "cron": str(self.cfg("cron") or "0 7 * * *").strip(),
            "timezone": str(self.cfg("timezone") or "").strip(),
        }


class Transform(Activity):
    """A deterministic map ``in: schemaA -> out: schemaB``. Its body can be LLM-
    generated from the two port schemas (§6 codegen). Inert when the schemas already
    match (e.g. the News Agent's str->str), which is why it is not in that slice."""

    kind = "transform"
    label = "Transform"

    def __init__(
        self,
        *,
        uid: Optional[str] = None,
        config: Optional[dict[str, Any]] = None,
        in_schema: DataSchema = ANY,
        out_schema: DataSchema = ANY,
    ) -> None:
        super().__init__(uid=uid, config=config)
        self._in = in_schema
        self._out = out_schema

    def get_schema(self) -> BlockSchema:
        return BlockSchema(
            kind=self.kind,
            category=self.category,
            label=self.label,
            ports=[Port("in", "in", self._in), Port("out", "out", self._out)],
            config=[ConfigField("script", "generated")],
        )

    def lower(self) -> dict[str, Any]:
        # No runtime-DSL field yet (the v0 record has no transform stage). A real
        # Transform contributes once the DSL/IR models it (Phase 3+). Inert here.
        return {}


# --------------------------------------------------------------------------- #
# Family: Control (Branch/Loop) — un-deferred in Phase 3 (the graph form). These
# have no *flat*-record fragment; they exist in the graph-form IR (ir.py) and are
# executed by the GraphExecutor via out-port routing. See design §3.2 / §7 Phase 3.
# --------------------------------------------------------------------------- #
class Branch(Activity):
    """Conditional routing: ``in`` -> one of several guarded ``out`` ports. The chosen
    port is decided at run time (by the branch handler), so downstream is data-driven —
    this is what a presence-based compiler can never express."""

    kind = "branch"
    category = "Control"
    label = "Branch"

    def get_schema(self) -> BlockSchema:
        return BlockSchema(
            kind=self.kind,
            category=self.category,
            label=self.label,
            ports=[Port("in", "in", ANY), Port("out", "out", ANY)],
            config=[
                ConfigField("branches", "json", default=["then", "else"]),  # out-port labels
                ConfigField("predicate", "json"),  # declarative rule chosen per branch (Phase-3, open)
            ],
        )

    def lower(self) -> dict[str, Any]:
        return {}  # graph-form only; no flat-record field


class Loop(Activity):
    """Bounded repetition: routes back to its body until a condition holds or
    ``max_iter`` is hit, then exits. ``max_iter`` is a hard cap (no runaway loops)."""

    kind = "loop"
    category = "Control"
    label = "Loop"

    def get_schema(self) -> BlockSchema:
        return BlockSchema(
            kind=self.kind,
            category=self.category,
            label=self.label,
            ports=[Port("in", "in", ANY), Port("out", "out", ANY)],
            config=[
                ConfigField("condition", "json"),
                ConfigField("max_iter", "integer", default=10),
            ],
        )

    def validate(self) -> list[str]:
        errors = super().validate()
        mi = self.cfg("max_iter", 10)
        if not isinstance(mi, int) or mi < 1:
            errors.append(f"{self.label}: max_iter must be an integer >= 1 (got {mi!r})")
        return errors

    def lower(self) -> dict[str, Any]:
        return {}  # graph-form only; no flat-record field


# --------------------------------------------------------------------------- #
# Family: Destination (in-only sink; base = target + channel).
# --------------------------------------------------------------------------- #
class Destination(Block):
    """In-only sink. Lowers (with the brain's result feeding it) to
    ``delivery: {channel, target}``. The channel is fixed per subclass; the target is
    config. Secrets (tokens) are NEVER here — they come from runtime config/env."""

    category = "Destination"
    channel: str = ""

    def get_schema(self) -> BlockSchema:
        return BlockSchema(
            kind=self.kind,
            category=self.category,
            label=self.label,
            ports=[Port("in", "in", STRING)],
            config=[ConfigField("target", "string", required=True)],
        )

    def lower(self) -> dict[str, Any]:
        return {"delivery": {"channel": self.channel, "target": self.cfg("target", "")}}


# --------------------------------------------------------------------------- #
# Composite — a Workflow-as-a-block (nesting). Its interface (get_schema) is its
# UNBOUND boundary; its inside is a graph of participants. This is the *explicit*
# nesting node — but composition is also a PROPERTY of Agent (design §3.2), so an
# Agent can equally be composed. Executed by wrapping the inner graph in a nested
# GraphExecutor (see executor.composite_handler).
# --------------------------------------------------------------------------- #
class Composite(Block):
    """A saved workflow referenced as one participant. ``inner`` is the graph-form IR
    it runs; the block's boundary ports are what the outside wires to."""

    kind = "composite"
    category = "Composite"
    label = "Workflow"

    def __init__(
        self,
        *,
        uid: Optional[str] = None,
        config: Optional[dict[str, Any]] = None,
        inner: Any = None,          # an IRGraph (its inside)
        in_schema: DataSchema = ANY,
        out_schema: DataSchema = ANY,
    ) -> None:
        super().__init__(uid=uid, config=config)
        self._inner = inner
        self._in = in_schema
        self._out = out_schema

    @property
    def inner(self) -> Any:
        return self._inner

    def get_schema(self) -> BlockSchema:
        return BlockSchema(
            kind=self.kind,
            category=self.category,
            label=self.label,
            ports=[Port("in", "in", self._in), Port("out", "out", self._out)],
            config=[ConfigField("workflow_ref", "string")],  # name/id of the saved workflow
        )

    def validate(self) -> list[str]:
        errors = super().validate()
        if self._inner is None and not self.cfg("workflow_ref"):
            errors.append(f"{self.label}: a Composite needs an inner graph or a workflow_ref")
        return errors

    def lower(self) -> dict[str, Any]:
        return {}  # graph-form only


class WhatsApp(Destination):
    kind = "whatsapp"
    label = "WhatsApp"
    channel = "whatsapp"


class TTS(Destination):
    kind = "tts"
    label = "TTS"
    channel = "tts"


class Bus(Destination):
    kind = "bus"
    label = "Bus"
    channel = "bus"
