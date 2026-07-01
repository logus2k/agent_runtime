"""Graph + lowering: a serialized composer graph -> the runtime DSL.

This is the part that fixes Patron's original sin. ``compile.js`` resolves stages by
node *presence* (``nodes.filter``), so the wires are decorative. Here we **trace the
links**: the agent is whatever the trigger connects to; the destination is whatever the
result chain actually reaches. A brain wired to nothing fails loudly instead of
silently emitting a record.

Input is the litegraph ``serialize()`` shape (the same fixture ``compile.js`` reads):

    nodes: [{ id, type, properties, inputs?, outputs? }]
    links: [[link_id, origin_id, origin_slot, target_id, target_slot, type], ...]

Output mirrors ``compile.js`` exactly so the two are interchangeable during migration:

    { "ok": true,  "dsl": {...flat record...}, "schedule": {cron, timezone}|null }
    { "ok": false, "errors": [ "...human-aimed...", ... ] }

The flat record is the degenerate linear graph (``dsl.py`` ``AgentRecord``). Branch/
Loop/Composite (the graph form) are deferred (Phases 3–4).
"""

from __future__ import annotations

from typing import Any, Optional

from .blocks import Agent
from .catalog import block_for_graph_type

DSL_VERSION = "0.1"

# Current serialized-graph node-type ids (the vocabulary the existing fixture uses).
T_TRIGGER = "patron/agent/trigger"
T_BRAIN = "patron/agent/brain"
T_TOOLS = "patron/agent/tools"
T_RAG = "patron/agent/rag"
T_GUARDRAIL = "patron/agent/guardrail"
T_DELIVER = "patron/agent/deliver"
_DEST_TYPES = {"patron/dest/whatsapp", "patron/dest/tts", "patron/dest/bus"}
# Pass-through stages in the result chain (carry no DSL field of their own here).
_PASS_THROUGH = {T_DELIVER}


class LoweringError(Exception):
    """The input is not a usable graph object (not the same as validation errors,
    which are returned in the ``errors`` list)."""


class Graph:
    """A composer document: blocks + edges, link-traced to the runtime DSL."""

    def __init__(self, serialized: dict[str, Any]) -> None:
        if not isinstance(serialized, dict):
            raise LoweringError(f"graph must be an object, got {type(serialized).__name__}")
        self.nodes: list[dict[str, Any]] = list(serialized.get("nodes") or [])
        self.links: list[list[Any]] = list(serialized.get("links") or [])
        self._by_id: dict[Any, dict[str, Any]] = {n.get("id"): n for n in self.nodes}

    # ---- link helpers (the actual tracing) ----
    def _out_links(self, node_id: Any) -> list[list[Any]]:
        # link = [id, origin_id, origin_slot, target_id, target_slot, type]
        return [lk for lk in self.links if len(lk) >= 4 and lk[1] == node_id]

    def _in_links(self, node_id: Any) -> list[list[Any]]:
        return [lk for lk in self.links if len(lk) >= 4 and lk[3] == node_id]

    def _node(self, node_id: Any) -> Optional[dict[str, Any]]:
        return self._by_id.get(node_id)

    def _of_type(self, node_type: str) -> list[dict[str, Any]]:
        return [n for n in self.nodes if n.get("type") == node_type]

    @staticmethod
    def _props(node: Optional[dict[str, Any]]) -> dict[str, Any]:
        return (node or {}).get("properties") or {}

    def _sources_into(self, node_id: Any, node_type: str) -> list[dict[str, Any]]:
        """Nodes of ``node_type`` whose output is wired INTO ``node_id`` (traced)."""
        out: list[dict[str, Any]] = []
        for lk in self._in_links(node_id):
            origin = self._node(lk[1])
            if origin is not None and origin.get("type") == node_type:
                out.append(origin)
        return out

    def _trace_to_destination(self, brain_id: Any) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]], list[str]]:
        """Follow the brain's result chain along real links until a destination node.

        Returns (destination_node, guardrail_nodes_in_chain, errors). Pass-through
        stages (Deliver) and Guardrails are walked over; the terminal must be a
        destination block."""
        errors: list[str] = []
        guardrails: list[dict[str, Any]] = []
        current = brain_id
        visited: set[Any] = {brain_id}
        while True:
            outs = self._out_links(current)
            if not outs:
                errors.append(
                    "the result chain is not connected to a destination "
                    "(WhatsApp / TTS / Bus) — wire the agent's output through to a channel"
                )
                return None, guardrails, errors
            if len(outs) > 1:
                errors.append(
                    f"node {current} fans out to {len(outs)} targets; the linear v0 "
                    f"lowering expects a single result path (branching is Phase 3)"
                )
            nxt = self._node(outs[0][3])
            if nxt is None:
                errors.append(f"dangling link to missing node id {outs[0][3]}")
                return None, guardrails, errors
            ntype = nxt.get("type")
            nid = nxt.get("id")
            if nid in visited:
                errors.append(f"cycle detected in the result chain at node {nid}")
                return None, guardrails, errors
            visited.add(nid)
            if ntype in _DEST_TYPES:
                return nxt, guardrails, errors
            if ntype == T_GUARDRAIL:
                guardrails.append(nxt)
            elif ntype in _PASS_THROUGH:
                pass  # Deliver: structural pass-through
            else:
                errors.append(
                    f"unexpected node type '{ntype}' between the agent and its "
                    f"destination (only Guardrail/Deliver may sit in the result chain)"
                )
            current = nid

    # ---- the lowering ----
    def lower(self) -> dict[str, Any]:
        errors: list[str] = []

        triggers = self._of_type(T_TRIGGER)
        brains = self._of_type(T_BRAIN)
        if len(triggers) != 1:
            errors.append(f"expected exactly one Trigger node, found {len(triggers)}")
        if len(brains) != 1:
            errors.append(f"expected exactly one Brain/Agent node, found {len(brains)}")
        if errors:
            return {"ok": False, "errors": errors}

        trigger_node, brain_node = triggers[0], brains[0]
        brain_id = brain_node.get("id")

        # Trace: trigger must reach the brain; the result chain must reach a channel.
        trig_targets = [self._node(lk[3]) for lk in self._out_links(trigger_node.get("id"))]
        if not any(t is not None and t.get("id") == brain_id for t in trig_targets):
            errors.append("the Trigger is not wired to the Agent (no traced path trigger → agent)")

        dest_node, guardrails, chain_errors = self._trace_to_destination(brain_id)
        errors.extend(chain_errors)

        # Build the Trigger + Destination blocks from the traced nodes.
        trigger = block_for_graph_type(T_TRIGGER, self._props(trigger_node))
        destination = block_for_graph_type(dest_node.get("type"), self._props(dest_node)) if dest_node else None
        if destination is None and dest_node is None:
            pass  # error already recorded by the trace
        elif destination is None:
            errors.append(f"unknown destination node type '{dest_node.get('type')}'")

        # Build the Agent by FOLDING the brain + traced capability nodes into config
        # (tools/rag/guardrail are config on the Agent, not separate participants).
        agent = self._build_agent(brain_node, guardrails)

        # Per-block validation (loud, human-aimed).
        for block in (trigger, agent, destination):
            if block is not None:
                errors.extend(block.validate())

        if errors:
            return {"ok": False, "errors": errors}

        # Merge fragments in flow order: trigger (id+trigger) → agent (brain…input) →
        # destination (delivery). Dict-merge; later fragments add keys, never clobber.
        dsl: dict[str, Any] = {"version": DSL_VERSION}
        for frag in (trigger.lower(), agent.lower(), destination.lower()):
            dsl.update(frag)

        schedule = trigger.schedule_spec()
        return {"ok": True, "dsl": dsl, "schedule": schedule}

    def to_ir(self):
        """Build the graph-form IR for the (currently linear) traced graph:
        trigger → agent → destination. Gives the linear case an executable IR path
        (walked by GraphExecutor) alongside the flat-record lowering; Branch/Loop
        extend this with extra nodes/ports (Phase 3). Raises LoweringError on a graph
        that can't be traced (same loudness as ``lower``)."""
        from .ir import IREdge, IRGraph, IRNode

        result = self.lower()
        if not result.get("ok"):
            raise LoweringError(
                "cannot build IR — graph does not lower: " + "; ".join(result.get("errors", []))
            )
        triggers = self._of_type(T_TRIGGER)
        brains = self._of_type(T_BRAIN)
        trigger_node, brain_node = triggers[0], brains[0]
        brain_id = brain_node.get("id")
        dest_node, _guards, _errs = self._trace_to_destination(brain_id)

        trig_id = f"trigger:{trigger_node.get('id')}"
        agent_id = f"agent:{brain_id}"
        dest_id = f"destination:{dest_node.get('id')}"
        dsl = result["dsl"]
        nodes = {
            trig_id: IRNode(trig_id, "trigger", dict(dsl.get("trigger", {}), id=dsl.get("id"))),
            agent_id: IRNode(agent_id, "agent", {k: dsl[k] for k in ("brain", "tools", "rag", "guardrails", "input") if k in dsl}),
            dest_id: IRNode(dest_id, "destination", dict(dsl.get("delivery", {}))),
        }
        edges = [IREdge(trig_id, agent_id), IREdge(agent_id, dest_id)]
        return IRGraph(nodes=nodes, edges=edges, entry=trig_id)

    def _build_agent(self, brain_node: dict[str, Any], guardrails: list[dict[str, Any]]) -> Agent:
        bp = self._props(brain_node)
        brain_id = brain_node.get("id")
        config: dict[str, Any] = {
            "persona": bp.get("persona", ""),
            "temperature": bp.get("temperature", 0.3),
            "max_tokens": bp.get("max_tokens", 1024),
            "input_template": bp.get("input_template", ""),
            "input_vars": bp.get("input_vars", ""),
        }
        # Fold a Tools node wired into the brain (capability → config).
        tools = self._sources_into(brain_id, T_TOOLS)
        if tools:
            tp = self._props(tools[0])
            config.update(
                tools_present=True,
                tools_server=tp.get("server", ""),
                tools_allow=tp.get("allow", ""),
                tools_max_rounds=tp.get("max_rounds", 3),
            )
        # Fold a RAG node wired into the brain.
        rags = self._sources_into(brain_id, T_RAG)
        if rags:
            rp = self._props(rags[0])
            config.update(
                rag_present=True,
                rag_rewriter=rp.get("rewriter", ""),
                rag_domains=rp.get("domains", ""),
                rag_use_graph=rp.get("use_graph", False),
            )
        # Fold a Guardrail found in the result chain.
        if guardrails:
            gp = self._props(guardrails[0])
            config.update(
                guard_present=True,
                guard_forbidden=gp.get("forbidden", ""),
                guard_min_confidence=gp.get("min_confidence", 0.5),
            )
        return Agent(uid=str(brain_id), config=config)


def lower_graph(serialized: dict[str, Any]) -> dict[str, Any]:
    """Lower a serialized composer graph to the runtime DSL. Mirrors compile.js's
    return shape: ``{ok, dsl, schedule}`` or ``{ok: False, errors}``."""
    return Graph(serialized).lower()
