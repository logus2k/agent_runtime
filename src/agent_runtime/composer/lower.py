"""Graph + lowering: a serialized composer graph -> the runtime DSL.

The graph is authored in the composer's OWN vocabulary — node ``type`` == ``Block.kind``
(``trigger`` / ``agent`` / ``whatsapp`` / …), a single ``flow`` wire between blocks, and
capabilities (tools/rag/guardrails) as CONFIG on the Agent. There is NO legacy adapter:
blocks are instantiated straight from the catalog by their type id.

We **trace the links** (not node presence): the agent is whatever the trigger connects
to; the destination is whatever the agent's output chain actually reaches. A block wired
to nothing fails loudly.

Input is the litegraph ``serialize()`` shape:

    nodes: [{ id, type, properties, inputs?, outputs? }]
    links: [[link_id, origin_id, origin_slot, target_id, target_slot, type], ...]

Output:

    { "ok": true,  "dsl": {...flat record...}, "schedule": {cron, timezone}|null }
    { "ok": false, "errors": [ "...human-aimed...", ... ] }

The flat record is the degenerate *linear* graph (``dsl.py`` ``AgentRecord``). Branch/
Loop/Composite live in the graph-form IR (``ir.py``) and execute via the GraphExecutor.
"""

from __future__ import annotations

from typing import Any, Optional

from .catalog import BLOCK_TYPES

DSL_VERSION = "0.1"

TRIGGER_KIND = "trigger"
AGENT_KIND = "agent"
DEST_KINDS = {"whatsapp", "tts", "bus"}
# Blocks that may sit between the agent and its destination as pass-throughs in the
# LINEAR lowering (they carry no flat-record field; a real Transform needs the graph
# form to be represented — it is inert in the flat record).
_PASS_THROUGH = {"transform"}


class LoweringError(Exception):
    """The input is not a usable graph object (distinct from validation errors, which
    are returned in the ``errors`` list)."""


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

    def _node(self, node_id: Any) -> Optional[dict[str, Any]]:
        return self._by_id.get(node_id)

    def _of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [n for n in self.nodes if n.get("type") == kind]

    @staticmethod
    def _props(node: Optional[dict[str, Any]]) -> dict[str, Any]:
        return (node or {}).get("properties") or {}

    def _trace_to_destination(
        self, start_id: Any
    ) -> tuple[Optional[dict[str, Any]], list[str]]:
        """Follow the flow from the agent along real links until a destination node."""
        errors: list[str] = []
        current = start_id
        visited: set[Any] = {start_id}
        while True:
            outs = self._out_links(current)
            if not outs:
                errors.append(
                    "the agent's output is not connected to a destination "
                    "(WhatsApp / TTS / Bus) — wire the agent's out to a channel"
                )
                return None, errors
            if len(outs) > 1:
                errors.append(
                    f"node {current} fans out to {len(outs)} targets; the linear v0 "
                    f"lowering expects a single flow (branching is the graph form)"
                )
            nxt = self._node(outs[0][3])
            if nxt is None:
                errors.append(f"dangling link to missing node id {outs[0][3]}")
                return None, errors
            kind, nid = nxt.get("type"), nxt.get("id")
            if nid in visited:
                errors.append(f"cycle detected in the flow at node {nid}")
                return None, errors
            visited.add(nid)
            if kind in DEST_KINDS:
                return nxt, errors
            if kind in _PASS_THROUGH:
                current = nid
                continue
            errors.append(
                f"unexpected node type '{kind}' between the agent and its destination "
                f"(only Transform may sit in the flow of a linear agent)"
            )
            current = nid

    def _block(self, node: dict[str, Any]):
        """Instantiate the block for a node straight from the catalog (type == kind).
        No adapter: the node's ``properties`` ARE the block's config."""
        cls = BLOCK_TYPES.get(node.get("type"))
        if cls is None:
            return None
        return cls(uid=str(node.get("id")), config=dict(self._props(node)))

    # ---- the lowering ----
    def lower(self) -> dict[str, Any]:
        errors: list[str] = []

        triggers = self._of_kind(TRIGGER_KIND)
        agents = self._of_kind(AGENT_KIND)
        if len(triggers) != 1:
            errors.append(f"expected exactly one Trigger node, found {len(triggers)}")
        if len(agents) != 1:
            errors.append(f"expected exactly one Agent node, found {len(agents)}")
        if errors:
            return {"ok": False, "errors": errors}

        trigger_node, agent_node = triggers[0], agents[0]
        agent_id = agent_node.get("id")

        # trigger must reach the agent (traced, not assumed).
        trig_targets = [self._node(lk[3]) for lk in self._out_links(trigger_node.get("id"))]
        if not any(t is not None and t.get("id") == agent_id for t in trig_targets):
            errors.append("the Trigger is not wired to the Agent (no traced path trigger → agent)")

        dest_node, chain_errors = self._trace_to_destination(agent_id)
        errors.extend(chain_errors)

        trigger = self._block(trigger_node)
        agent = self._block(agent_node)
        destination = self._block(dest_node) if dest_node else None
        if dest_node is not None and destination is None:
            errors.append(f"unknown destination node type '{dest_node.get('type')}'")

        for block in (trigger, agent, destination):
            if block is not None:
                errors.extend(block.validate())

        if errors:
            return {"ok": False, "errors": errors}

        # Merge fragments in flow order: trigger (id+trigger) → agent (brain…input) →
        # destination (delivery).
        dsl: dict[str, Any] = {"version": DSL_VERSION}
        for frag in (trigger.lower(), agent.lower(), destination.lower()):
            dsl.update(frag)

        return {"ok": True, "dsl": dsl, "schedule": trigger.schedule_spec()}

    def to_workflow_ir(self):
        """Build the graph-form IR by a GENERAL topology walk from the single trigger
        over the real links — supporting **N agents** chained edge-to-edge and arbitrary
        destinations. Distinct from ``to_ir()`` (which is the degenerate linear path via
        ``AgentRecord``); this path never routes through the flat record, so a 2-agent
        workflow (which cannot lower to a single ``AgentRecord``) is representable.

        Each emitted ``IRNode`` carries the block's own ``lower()`` fragment in
        ``config``. For an ``agent`` node the fragment is wrapped as a fully-formed
        ``AgentRecord``-shaped dict under ``config["record"]`` so the runner's per-node
        ``h_agent`` reuses ``run_brain`` verbatim (agent-1's answer becomes agent-2's
        task). ``entry`` is the trigger node id. Raises ``LoweringError`` on no/many
        triggers or a dangling link.
        """
        from .ir import IREdge, IRGraph, IRNode

        triggers = self._of_kind(TRIGGER_KIND)
        if len(triggers) != 1:
            raise LoweringError(
                f"expected exactly one Trigger node, found {len(triggers)}"
            )
        trigger_node = triggers[0]

        def node_key(node: dict[str, Any]) -> str:
            return f"{node.get('type')}:{node.get('id')}"

        nodes: dict[str, IRNode] = {}
        edges: list[IREdge] = []
        errors: list[str] = []

        # BFS over the real links from the trigger. Emit one IRNode per reachable node
        # (kind = node type) and one IREdge per link (port = the source out-slot label).
        seen: set[Any] = set()
        queue: list[dict[str, Any]] = [trigger_node]
        while queue:
            node = queue.pop(0)
            nid = node.get("id")
            if nid in seen:
                continue
            seen.add(nid)

            key = node_key(node)
            block = self._block(node)
            if block is None:
                errors.append(f"unknown node type '{node.get('type')}' (id {nid})")
                continue
            errors.extend(block.validate())

            kind = node.get("type")
            frag = block.lower()
            if kind == AGENT_KIND:
                config = {"record": self._agent_record(node, frag)}
            elif kind == TRIGGER_KIND:
                config = dict(frag.get("trigger", {}), id=frag.get("id"))
            elif kind in DEST_KINDS:
                config = dict(frag.get("delivery", {}))
            else:
                config = dict(frag)
            nodes[key] = IRNode(key, kind, config)

            for lk in self._out_links(nid):
                target = self._node(lk[3])
                if target is None:
                    errors.append(f"dangling link to missing node id {lk[3]}")
                    continue
                # port = the source out-slot label; the chain slice is all "out".
                port = self._out_slot_label(node, lk[2])
                edges.append(IREdge(key, node_key(target), port))
                queue.append(target)

        if errors:
            raise LoweringError("cannot build workflow IR: " + "; ".join(errors))
        # Defensive: drop any edge whose target was rejected (no-op when errorless).
        edges = [e for e in edges if e.dst in nodes]
        return IRGraph(nodes=nodes, edges=edges, entry=node_key(trigger_node))

    @staticmethod
    def _out_slot_label(node: dict[str, Any], slot: Any) -> str:
        """The source out-port label for a link's origin slot. Named outputs (Branch/
        Loop) carry a label; a plain single-flow output is ``"out"``."""
        outputs = (node or {}).get("outputs") or []
        try:
            name = outputs[int(slot)].get("name")
        except (IndexError, TypeError, ValueError, AttributeError):
            name = None
        return name or "out"

    def _agent_record(self, node: dict[str, Any], frag: dict[str, Any]) -> dict[str, Any]:
        """Wrap an Agent block's ``lower()`` fragment into a fully-formed
        ``AgentRecord``-shaped dict. ``run_brain`` reads only brain/tools/name, so an
        intermediate (non-terminal) agent gets a synthetic, never-used ``delivery`` to
        satisfy the record shape — delivery for a chain happens at the destination node,
        not at the agent."""
        record: dict[str, Any] = {"version": DSL_VERSION}
        record.update(frag)
        record["uid"] = "00000000-0000-4000-8000-000000000000"
        record["name"] = frag.get("id") or str(node.get("id"))
        record.pop("id", None)
        # A synthetic delivery so the AgentRecord validates; the executor delivers via the
        # actual destination node, never through this stub.
        record.setdefault("delivery", {"channel": "bus", "target": "unused"})
        return record

    def to_ir(self):
        """Build the graph-form IR (trigger → agent → destination) for execution by the
        GraphExecutor. Raises LoweringError on a graph that can't be traced."""
        from .ir import IREdge, IRGraph, IRNode

        result = self.lower()
        if not result.get("ok"):
            raise LoweringError(
                "cannot build IR — graph does not lower: " + "; ".join(result.get("errors", []))
            )
        trigger_node = self._of_kind(TRIGGER_KIND)[0]
        agent_node = self._of_kind(AGENT_KIND)[0]
        dest_node, _errs = self._trace_to_destination(agent_node.get("id"))

        dsl = result["dsl"]
        trig_id = f"trigger:{trigger_node.get('id')}"
        agent_id = f"agent:{agent_node.get('id')}"
        dest_id = f"{dest_node.get('type')}:{dest_node.get('id')}"
        nodes = {
            trig_id: IRNode(trig_id, "trigger", dict(dsl.get("trigger", {}), id=dsl.get("id"))),
            agent_id: IRNode(
                agent_id, "agent",
                {k: dsl[k] for k in ("brain", "tools", "rag", "guardrails", "input") if k in dsl},
            ),
            dest_id: IRNode(dest_id, dest_node.get("type"), dict(dsl.get("delivery", {}))),
        }
        edges = [IREdge(trig_id, agent_id), IREdge(agent_id, dest_id)]
        return IRGraph(nodes=nodes, edges=edges, entry=trig_id)


def lower_graph(serialized: dict[str, Any]) -> dict[str, Any]:
    """Lower a serialized composer graph to the runtime DSL."""
    return Graph(serialized).lower()
