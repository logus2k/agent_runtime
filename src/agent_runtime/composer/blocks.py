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
# Helpers shared by leaves (CSV allow-lists + JSON input-vars parsing).
# --------------------------------------------------------------------------- #
def _csv(value: Any) -> list[str]:
    """'a, b ,' -> ['a','b']  (split on comma, trim, drop empties)."""
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
                # Identity/status lead the panel (authoring UX): enabled, persona, description.
                ConfigField("enabled", "boolean", control="boolean", default=True),
                # Generic grounded picker: control "resource-ref" + kind = the resource id
                # ("preset") → the editor renders a dropdown from /resources/preset. No bespoke code.
                ConfigField("persona", "preset", required=True, control="resource-ref",
                            placeholder="agent_server preset (selects the model)"),
                ConfigField("description", "string", control="textarea",
                            placeholder="what this agent does"),
                ConfigField("temperature", "number", control="number", min=0, max=2, default=0.3),
                ConfigField("max_tokens", "integer", control="number", min=1, default=1024,
                            label="max tokens"),
                ConfigField("input_template", "string", control="template", label="input template",
                            placeholder="The task prompt; may reference {vars}"),
                ConfigField("input_vars", "json", control="json", label="input vars",
                            placeholder='{"n": 5, "topic": "AI agents"}'),
                # No `tools_server` field: the MCP server is encoded in each tool's
                # `<server>__tool` prefix (the picker returns prefixed names), so lowering
                # derives it. Making the user type "mcp" was redundant + a typo footgun.
                # Generic multi-select picker: control "resource-ref" + kind = resource id
                # ("mcp-tool", which the descriptor declares multi=True). No bespoke code.
                ConfigField("tools_allow", "mcp-tool", control="resource-ref", label="tools (allow-list)",
                            placeholder="server__tool, server__tool"),
                ConfigField("tools_max_rounds", "integer", control="number", min=1, default=3,
                            label="tools max rounds"),
                # Generic multi-select picker: control "resource-ref" + kind = resource id
                # ("skill", declared multi=True) → the editor renders a checklist from
                # /resources/skill, exactly like the Tools picker (§8.3). No bespoke code.
                ConfigField("skills_allow", "skill", control="resource-ref", label="skills",
                            placeholder="skill-name, skill-name"),
                ConfigField("skills_context", "string", control="text", label="skills context",
                            placeholder="trigger-condition, trigger-condition"),
                ConfigField("memory", "enum", values=["none", "thread_window"], default="none",
                            control="select", label="memory policy"),
                ConfigField("memory_max_turns", "integer", control="number", min=1, default=20,
                            label="memory max turns"),
                # --- Loop (§8.4): the OUTER repeat loop around the whole agent action.
                # Distinct from tools_max_rounds (the Brain's INNER tool loop). Types are
                # SEPARATE (not composable): off / counter / expression / judge. Per-type
                # fields below are only meaningful for their type; lowering emits `loop`
                # only when the type is not "off".
                ConfigField("loop_type", "enum",
                            values=["off", "counter", "expression", "judge"],
                            default="off", control="select", label="loop type"),
                ConfigField("loop_n", "integer", control="number", min=1, default=1,
                            label="loop count (counter)"),
                ConfigField("loop_expression", "string", control="text",
                            label="loop expression",
                            placeholder="stop when this matches the outcome (/regex/ for regex)"),
                ConfigField("loop_max_iter", "integer", control="number", min=1, default=10,
                            label="loop max iterations (cap)"),
                ConfigField("loop_iteration_input", "enum", values=["same", "previous"],
                            default="same", control="select", label="loop iteration input"),
                # judge sub-config (only for loop type "judge"): the embedded Judge persona
                # + how its verdict text is read (expression on output OR a structured field).
                ConfigField("loop_judge_persona", "preset", control="resource-ref",
                            label="judge persona (judge)",
                            placeholder="agent_server preset that judges the outcome"),
                ConfigField("loop_verdict_read", "enum", values=["expression", "field"],
                            default="expression", control="select", label="judge verdict read"),
                ConfigField("loop_verdict_expression", "string", control="text",
                            label="judge verdict expression",
                            placeholder="validate when this matches the judge output"),
                ConfigField("loop_verdict_field", "string", control="text",
                            label="judge verdict field",
                            placeholder="dotted JSON path, e.g. result.passed"),
                ConfigField("loop_judge_template", "string", control="template",
                            label="judge input template",
                            placeholder="optional; binds {outcome} and {input}"),
                # (enabled / persona / description are shown first, above.)
                # --- optional sampling overrides (beyond temperature/max_tokens) ---
                ConfigField("top_p", "number", control="number", label="top_p"),
                ConfigField("top_k", "integer", control="number", label="top_k"),
                ConfigField("min_p", "number", control="number", label="min_p"),
                # --- optional RAG capability ---
                ConfigField("rag_rewriter", "string", control="text", label="rag rewriter"),
                ConfigField("rag_domains", "string", control="text", label="rag domains",
                            placeholder="domain, domain"),
                ConfigField("rag_use_graph", "boolean", control="boolean", label="rag use graph"),
                # --- optional guardrails capability ---
                ConfigField("guard_forbidden", "string", control="text", label="guardrail forbidden",
                            placeholder="pattern, pattern"),
                ConfigField("guard_min_confidence", "number", control="number", min=0, max=1,
                            label="guardrail min confidence"),
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
        llm: dict[str, Any] = {
            "temperature": float(self.cfg("temperature", 0.3)),
            "max_tokens": int(self.cfg("max_tokens", 1024)),
        }
        # Optional sampling overrides — emitted ONLY when set, so an unset field is absent
        # (matches the record, whose Optional llm params are None/omitted).
        for key, caster in (("top_p", float), ("top_k", int), ("min_p", float)):
            v = self.cfg(key)
            if v not in (None, ""):
                llm[key] = caster(v)
        frag: dict[str, Any] = {"brain": {"persona": self.cfg("persona", ""), "llm": llm}}
        # Agent-level metadata (top-level record fields).
        if self.cfg("description") not in (None, ""):
            frag["description"] = self.cfg("description")
        frag["enabled"] = bool(self.cfg("enabled", True))
        # Optional capabilities — emitted only when the Agent's config carries them
        # (capabilities are config fields ON the agent, not separate nodes). Presence is
        # inferred from the capability's own keys. Order: rag, tools, guardrails, input.
        if self.cfg("rag_rewriter") or self.cfg("rag_domains") or self.cfg("rag_use_graph"):
            frag["rag"] = {
                "rewriter": self.cfg("rag_rewriter") or None,
                "domains": _csv(self.cfg("rag_domains")),
                "use_graph": bool(self.cfg("rag_use_graph")),
            }
        allow = _csv(self.cfg("tools_allow"))
        if allow:
            # The server key is encoded in the tool prefix (<server>__tool) — one MCP server
            # per agent today. Derive it instead of carrying a redundant free-text field.
            server = allow[0].split("__", 1)[0]
            frag["tools"] = {
                "server": server,
                "allow": allow,
                "max_rounds": int(self.cfg("tools_max_rounds", 3)),
            }
        # Skills (§8.3): the selected skill names + optional context conditions. Emitted
        # only when the Agent selected skills — it stays ON the agent (dynamic prompt
        # assembly), never a wired node.
        skills_allow = _csv(self.cfg("skills_allow"))
        if skills_allow:
            frag["skills"] = {
                "allow": skills_allow,
                "context": _csv(self.cfg("skills_context")),
            }
        if self.cfg("guard_forbidden") or self.cfg("guard_min_confidence") not in (None, ""):
            frag["guardrails"] = {
                "forbidden": _csv(self.cfg("guard_forbidden")),
                "min_confidence": float(self.cfg("guard_min_confidence") or 0.5),
            }
        # Loop (§8.4): the OUTER repeat loop. Emitted only when the type is not "off".
        # Per-type fields are only folded in for their type (the DSL validates them).
        loop_type = self.cfg("loop_type", "off") or "off"
        if loop_type != "off":
            loop_frag: dict[str, Any] = {
                "type": loop_type,
                "max_iter": int(self.cfg("loop_max_iter", 10)),
                "iteration_input": self.cfg("loop_iteration_input", "same") or "same",
            }
            if loop_type == "counter":
                loop_frag["n"] = int(self.cfg("loop_n", 1))
            elif loop_type == "expression":
                loop_frag["expression"] = self.cfg("loop_expression", "") or ""
            elif loop_type == "judge":
                verdict_read = self.cfg("loop_verdict_read", "expression") or "expression"
                verdict: dict[str, Any] = {"read": verdict_read}
                if verdict_read == "expression":
                    verdict["expression"] = self.cfg("loop_verdict_expression", "") or ""
                else:
                    verdict["field"] = self.cfg("loop_verdict_field", "") or ""
                judge: dict[str, Any] = {
                    "persona": self.cfg("loop_judge_persona", "") or "",
                    "verdict": verdict,
                }
                if self.cfg("loop_judge_template") not in (None, ""):
                    judge["input_template"] = self.cfg("loop_judge_template")
                loop_frag["judge"] = judge
            frag["loop"] = loop_frag
        frag["memory"] = {
            "policy": self.cfg("memory", "none") or "none",
            "max_turns": int(self.cfg("memory_max_turns", 20)),
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
    """Boundary source: fires the agent on a **schedule**. ``out`` only. Carries the agent
    id + the schedule — the *when* lives beside the record as a scheduler job, not inside it.

    The schedule has a ``schedule_mode`` selecting which of the scheduler's three trigger
    kinds to use (all natively supported by agent_scheduler):

      * ``cron``     — a recurring cron expression (+ optional IANA timezone);
      * ``interval`` — every ``interval_value`` × ``interval_unit`` (seconds…weeks);
      * ``date``     — a single one-off run at ``run_date`` (ISO 8601).

    (The old ``channel`` trigger type was removed — it established no firing binding and
    never fired. Reactive/inbound flows use a Web/File/STT initiator instead.)"""

    kind = "trigger"
    label = "Trigger"

    def get_schema(self) -> BlockSchema:
        return BlockSchema(
            kind=self.kind,
            category=self.category,
            label=self.label,
            ports=[Port("out", "out", ANY)],
            config=[
                # No agent_id: in the graph-deploy model the Project uid is the identity and the
                # schedule key (proj-<uid>); firing routes by record_uid, so the Trigger carries
                # only the schedule (+ optional seed task) — no per-agent id.
                ConfigField("schedule_mode", "enum", values=["cron", "interval", "date"],
                            default="cron", control="select", label="schedule mode"),
                # --- cron mode ---
                ConfigField("cron", "string", default="0 7 * * *", control="text",
                            label="cron expression", placeholder="min hour dom month weekday"),
                ConfigField("timezone", "string", control="text", placeholder="e.g. Europe/Lisbon"),
                # --- interval mode ---
                ConfigField("interval_value", "number", default=30, control="number",
                            label="every (interval)"),
                ConfigField("interval_unit", "enum",
                            values=["seconds", "minutes", "hours", "days", "weeks"],
                            default="minutes", control="select", label="interval unit"),
                # --- date (one-off) mode ---
                ConfigField("run_date", "string", control="text", label="run at (one-off)",
                            placeholder="2026-08-01T09:00"),
                # Optional SEED for a fire (firing-contract data.task): a fixed query/message
                # the scheduled workflow starts from — feeds RAG-pre and the Agent's {input}.
                # Blank = no seed (the Agent uses its own input_template).
                ConfigField("task", "string", control="text", label="task / query (seed)",
                            placeholder="e.g. latest AI-safety papers"),
            ],
        )

    def lower(self) -> dict[str, Any]:
        return {
            "id": self.cfg("agent_id", "untitled-agent"),
            "trigger": {"type": "schedule"},
        }

    def schedule_spec(self) -> dict[str, Any]:
        """The scheduler-job side: ``{trigger_type, trigger_args}`` built from
        ``schedule_mode`` (agent_scheduler's native contract). cron defaults to
        '0 7 * * *'; an empty timezone means UTC."""
        mode = str(self.cfg("schedule_mode", "cron") or "cron").strip()
        if mode == "interval":
            unit = str(self.cfg("interval_unit", "minutes") or "minutes").strip()
            try:
                value = int(self.cfg("interval_value", 0) or 0)
            except (TypeError, ValueError):
                value = 0
            return {"trigger_type": "interval", "trigger_args": {unit: value}}
        if mode == "date":
            return {"trigger_type": "date",
                    "trigger_args": {"run_date": str(self.cfg("run_date") or "").strip()}}
        args: dict[str, Any] = {"cron_expression": str(self.cfg("cron") or "0 7 * * *").strip()}
        tz = str(self.cfg("timezone") or "").strip()
        if tz:
            args["timezone"] = tz
        return {"trigger_type": "cron", "trigger_args": args}


# --------------------------------------------------------------------------- #
# New boundary SOURCES (§8, §9.3.1): File / Web / Speech-to-Text initiators.
#
# These are boundary sources exactly like ``Trigger`` — ``out`` only, and INERT in the
# graph execution: they carry NO flat-record field and do no work at run time. Their
# firing happens EXTERNALLY (a folder-watch service, an HTTP-ingress service, an STT
# front-end) which emits the bus event that drives the farm; the block only carries the
# *binding* to that external source (the watched path, the route, the STT stream id).
#
# They lower to the ``initiator`` graph-node kind (§9.3.1) — several INDEPENDENT
# initiator block types, no "family" abstraction. The base collects the shared shape.
# --------------------------------------------------------------------------- #
class Initiator(Activity):
    """Base for a boundary source that fires the workflow from OUTSIDE. ``out`` only;
    inert in graph execution. Subclasses declare their own binding config; all lower to
    a ``trigger`` fragment whose ``type`` is ``channel`` (event-driven, not a schedule).

    A subclass sets ``kind``/``label`` and (optionally) extends ``get_schema``'s config
    with its own binding fields (watched path, route, stream id)."""

    category = "Activity"
    label = "Initiator"

    # Extra binding fields the concrete initiator adds beyond agent_id (subclass hook).
    def _binding_fields(self) -> list[ConfigField]:
        return []

    def get_schema(self) -> BlockSchema:
        return BlockSchema(
            kind=self.kind,
            category=self.category,
            label=self.label,
            ports=[Port("out", "out", STRING)],
            config=[
                # No agent_id: the Project uid is the identity; firing routes by record_uid.
                # The initiator carries only its binding (path / route / stream).
                *self._binding_fields(),
            ],
        )

    def lower(self) -> dict[str, Any]:
        # Boundary source: contributes the record id + a channel-type trigger. The
        # per-source binding (path/route/stream) is management-plane detail, carried on
        # the graph node's config, NOT in the flat runtime record (which has no field for
        # it) — hence inert here beyond id + trigger.
        return {
            "id": self.cfg("agent_id", "untitled-agent"),
            "trigger": {"type": "channel"},
        }


class FileInitiator(Initiator):
    """Fires when a new/changed file is detected in a watched folder (e.g. PDF →
    vector-DB ingestion), §9.3.1. Backed by an external folder-watch service that emits
    the bus event; this block only carries the watch binding."""

    kind = "file_initiator"
    label = "File Initiator"

    def _binding_fields(self) -> list[ConfigField]:
        return [
            ConfigField("watch_path", "string", required=True, control="text",
                        label="watch path", default="/watched/in", placeholder="/watched/in"),
            ConfigField("match", "string", control="text", label="match patterns",
                        placeholder="*.pdf, *.txt"),
        ]


class WebInitiator(Initiator):
    """Fires when a request hits a configured HTTP route (expose a workflow to web
    clients/services), §9.3.1. Backed by an external HTTP-ingress service. "Web" does
    NOT imply public — auth/exposure live at the nginx/OAuth2Proxy edge, not here."""

    kind = "web_initiator"
    label = "Web Initiator"

    def _binding_fields(self) -> list[ConfigField]:
        return [
            ConfigField("route", "string", required=True, control="text",
                        label="route", placeholder="/hooks/my-workflow"),
            ConfigField("method", "enum", values=["POST", "GET", "PUT"], default="POST",
                        control="select", label="method"),
        ]


class SttInitiator(Initiator):
    """Fires when a speech-to-text front-end produces a transcript. Boundary source; the
    external STT service emits the bus event with the transcript as the seed task."""

    kind = "stt_initiator"
    label = "Speech-to-Text"

    def _binding_fields(self) -> list[ConfigField]:
        return [
            ConfigField("stream_id", "string", required=True, control="text",
                        label="stream id", placeholder="the STT stream this listens on"),
            ConfigField("language", "string", control="text", label="language",
                        placeholder="e.g. en, pt"),
        ]


class ConsoleSend(Initiator):
    """A MANUAL initiator for testing/debugging: fires the workflow on demand with a message
    typed in Patron's Console panel (``POST /admin/projects/<uid>/fire`` — the firing seed).
    Carries no binding — it is fired by a button, not an external event, so Deploy creates
    no scheduler/ingress binding for it. The ``message`` is transient UI (sent at click
    time), not part of the deployed record."""

    kind = "console_send"
    label = "Console (Send)"

    def get_schema(self) -> BlockSchema:
        # A manual console has NO external binding, so it drops the generic 'agent id' field.
        # Its only config is the message to send — editable here OR typed in the Send prompt.
        return BlockSchema(
            kind=self.kind,
            category=self.category,
            label=self.label,
            ports=[Port("out", "out", STRING)],
            config=[
                ConfigField("message", "string", control="textarea", label="message",
                            placeholder="the message to send (or type it when you click Send ▶)"),
            ],
        )


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
            config=[ConfigField("script", "generated", control="textarea",
                                 placeholder="generated mapping code (see §6 codegen)")],
        )

    def lower(self) -> dict[str, Any]:
        # No runtime-DSL field yet (the v0 record has no transform stage). A real
        # Transform contributes once the DSL/IR models it (Phase 3+). Inert here.
        return {}


# --------------------------------------------------------------------------- #
# Family: Data sources (Vector/Graph Database) — STANDALONE retrieval blocks that
# QUERY a DB and emit the results into the flow (NOT agent-coupled — distinct from
# RAG-pre, which is Agent config that injects into the prompt). Runtime handlers
# ``h_vector_query`` / ``h_graph_query`` reuse the same retrieval fns as RAG-pre.
# --------------------------------------------------------------------------- #
class VectorDatabase(Block):
    """Query a dense vector corpus (noted-rag ``<domain>__corpus``) and OUTPUT the ranked
    passages. Uses the incoming value (the workflow seed) as the query, or a fixed ``query``."""

    kind = "vector_query"
    category = "Block"
    label = "Vector Database"

    def get_schema(self) -> BlockSchema:
        return BlockSchema(
            kind=self.kind,
            category=self.category,
            label=self.label,
            ports=[Port("in", "in", STRING), Port("out", "out", STRING)],
            config=[
                ConfigField("domain", "string", required=True, control="text",
                            label="domain", placeholder="e.g. cv  (collection <domain>__corpus)"),
                ConfigField("top_k", "integer", control="number", min=1, default=5, label="top k"),
                ConfigField("query", "string", control="text", label="query (blank = use input)",
                            placeholder="fixed query, or blank to query with the incoming value"),
            ],
        )

    def lower(self) -> dict[str, Any]:
        cfg: dict[str, Any] = {"domain": self.cfg("domain", "") or ""}
        if self.cfg("top_k") not in (None, ""):
            cfg["top_k"] = int(self.cfg("top_k"))
        if self.cfg("query"):
            cfg["query"] = self.cfg("query")
        return cfg


class GraphDatabase(Block):
    """Query a knowledge graph (noted-graph ``/research/<domain>/retrieve``) and OUTPUT the
    entities/relationships. Query = the incoming value, or a fixed ``query``."""

    kind = "graph_query"
    category = "Block"
    label = "Graph Database"

    def get_schema(self) -> BlockSchema:
        return BlockSchema(
            kind=self.kind,
            category=self.category,
            label=self.label,
            ports=[Port("in", "in", STRING), Port("out", "out", STRING)],
            config=[
                ConfigField("domain", "string", required=True, control="text",
                            label="domain", placeholder="e.g. cv"),
                ConfigField("query", "string", control="text", label="query (blank = use input)",
                            placeholder="fixed query, or blank to query with the incoming value"),
            ],
        )

    def lower(self) -> dict[str, Any]:
        cfg: dict[str, Any] = {"domain": self.cfg("domain", "") or ""}
        if self.cfg("query"):
            cfg["query"] = self.cfg("query")
        return cfg


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
                ConfigField("branches", "json", default=["then", "else"], control="json",
                            placeholder='["then", "else"]'),  # out-port labels
                ConfigField("predicate", "json", control="textarea",
                            placeholder="declarative rule (Phase-3, open)"),
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
                ConfigField("condition", "json", control="text",
                            placeholder="loop-until condition"),
                ConfigField("max_iter", "integer", default=10, control="number", min=1,
                            label="max iter"),
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
    # How the editor renders the `target` field. Subclasses override to a grounded picker
    # (e.g. WhatsApp → a dropdown of real Groups/Contacts + "type an id"). Metadata-driven:
    # the block declares the control, the editor renders the matching picker.
    target_control: str = "text"
    target_kind: str = "string"   # ConfigField kind; for a resource-ref picker = the resource id

    def get_schema(self) -> BlockSchema:
        return BlockSchema(
            kind=self.kind,
            category=self.category,
            label=self.label,
            ports=[Port("in", "in", STRING)],
            config=[
                # Human label for the target, auto-filled by the grounded picker (e.g. the
                # WhatsApp group name). Shown FIRST (name before id). Display only; lowered
                # only when set.
                ConfigField("target_name", "string", control="text", label="target name (display)",
                            placeholder="friendly name (auto-filled when picked)"),
                ConfigField("target", self.target_kind, required=True, control=self.target_control,
                            placeholder="destination id (chat / stream / voice)"),
            ],
        )

    def lower(self) -> dict[str, Any]:
        delivery: dict[str, Any] = {"channel": self.channel, "target": self.cfg("target", "")}
        name = self.cfg("target_name", "")
        if name:
            delivery["target_name"] = name
        return {"delivery": delivery}


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
            config=[ConfigField("workflow_ref", "string", control="text", label="workflow ref",
                                 placeholder="name/id of the saved workflow")],
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
    # Generic grounded picker: resource "wa-target" (declares group_by/allow_free/sets) → the
    # editor renders a Groups/Contacts dropdown + type-an-id + auto-fills target_name. No bespoke code.
    target_control = "resource-ref"
    target_kind = "wa-target"


class TTS(Destination):
    kind = "tts"
    label = "TTS"
    channel = "tts"


class Bus(Destination):
    kind = "bus"
    label = "Bus"
    channel = "bus"


# --------------------------------------------------------------------------- #
# New SINKS (§8, §7.1): File / Web destinations. In-only sinks like the other
# Destinations — File writes the outcome to a file; Web calls an outbound Web API with
# the outcome. Both lower to ``delivery: {channel, target}`` (the routing key is the
# path / URL) and are delivered by the executor's destination handler (mocked IO in
# tests). Distinct from the File/Web *initiators* (which fire the workflow, above).
# --------------------------------------------------------------------------- #
class FileDestination(Destination):
    """Writes the workflow outcome to a file (§8). ``target`` is the file path."""

    kind = "file_destination"
    label = "File Destination"
    channel = "file"

    def get_schema(self) -> BlockSchema:
        return BlockSchema(
            kind=self.kind,
            category=self.category,
            label=self.label,
            # A pass-through OUT so File Destination can persist AND hand its content onward
            # (the runtime returns the delivered value; the executor broadcasts to successors).
            ports=[Port("in", "in", STRING), Port("out", "out", STRING)],
            config=[
                ConfigField("target", "string", required=True, control="text",
                            label="file path", default="/watched/out/result.txt",
                            placeholder="/watched/out/result.txt"),
                ConfigField("mode", "enum", values=["overwrite", "append"],
                            default="overwrite", control="select", label="write mode"),
            ],
        )

    def lower(self) -> dict[str, Any]:
        delivery = super().lower()["delivery"]
        delivery["mode"] = self.cfg("mode", "overwrite") or "overwrite"
        return {"delivery": delivery}


class WebDestination(Destination):
    """Calls an outbound Web API with the workflow outcome (§8). ``target`` is the URL."""

    kind = "web_destination"
    label = "Web Destination"
    channel = "web"

    def get_schema(self) -> BlockSchema:
        return BlockSchema(
            kind=self.kind,
            category=self.category,
            label=self.label,
            ports=[Port("in", "in", STRING)],
            config=[
                ConfigField("target", "string", required=True, control="text",
                            label="url", placeholder="https://api.example.com/hook"),
                ConfigField("method", "enum", values=["POST", "PUT", "PATCH"],
                            default="POST", control="select", label="method"),
            ],
        )

    def lower(self) -> dict[str, Any]:
        delivery = super().lower()["delivery"]
        delivery["method"] = self.cfg("method", "POST") or "POST"
        return {"delivery": delivery}


class ConsoleReceive(Destination):
    """A DISPLAY sink for testing/debugging: shows the content that reaches it live in
    Patron's Console panel (pushed via SSE per node). No external target — the 'delivery'
    is to the browser. Pass-through, so it can also sit mid-flow (persist/observe + continue)."""

    kind = "console_receive"
    label = "Console (Receive)"
    channel = "console"

    def get_schema(self) -> BlockSchema:
        return BlockSchema(
            kind=self.kind,
            category=self.category,
            label=self.label,
            ports=[Port("in", "in", STRING), Port("out", "out", STRING)],
            config=[
                ConfigField("label", "string", control="text", label="label",
                            placeholder="optional label for this console"),
            ],
        )

    def lower(self) -> dict[str, Any]:
        # No external target; the label is carried as target for provenance.
        return {"delivery": {"channel": "console", "target": self.cfg("label", "") or ""}}
