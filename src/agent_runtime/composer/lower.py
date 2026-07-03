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
DEST_KINDS = {"whatsapp", "tts", "bus", "file_destination", "web_destination"}
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


# --------------------------------------------------------------------------- #
# Project -> GraphRecord lowering (Phase 05, §9.3).
#
# A Patron **Project** (a composition of blocks + typed edges, litegraph
# ``serialize()`` shape) lowers 1:1 to a single ``GraphRecord`` (dsl_graph):
# agents/rag/guardrails/destinations are NODES, the initiator is the entry node,
# and the composition's links become typed EDGES. Unlike ``lower()`` (which
# collapses to a flat single-agent ``AgentRecord``), this preserves the graph
# form: N agents, fan-in/fan-out, multiple destinations (§7.2).
#
# Validation is **advisory** (§9.3): warnings are computed and returned but a
# deploy is NEVER refused — the record is always built if it can be built at all.
# --------------------------------------------------------------------------- #

# Composition node type (== Block.kind) -> GraphRecord node kind. Only the kinds the
# GraphRecord model + graph executor understand are mapped here; anything else (e.g.
# ``transform``, which is inert in the runtime record) is UNSUPPORTED — it is skipped
# with an advisory warning rather than crashing the deploy (§9.3).
_KIND_MAP: dict[str, str] = {
    "trigger": "initiator",
    # New boundary SOURCES (§9.3.1) — all lower to the initiator graph-node kind.
    "file_initiator": "initiator",
    "web_initiator": "initiator",
    "stt_initiator": "initiator",
    "agent": "agent",
    "rag": "rag",
    "guardrail": "guardrail",
    # Standalone data-source query blocks (emit results into the flow).
    "vector_query": "vector_query",
    "graph_query": "graph_query",
    "whatsapp": "destination",
    "tts": "destination",
    "bus": "destination",
    # New SINKS (§8) — File writes a file; Web calls an outbound API.
    "file_destination": "destination",
    "web_destination": "destination",
}

# Composition block kinds that are recognized but produce NO runtime GraphRecord node —
# they lower to nothing (inert) and are skipped-with-warning, never a crash (§9.3). A real
# Transform needs graph-form Transform support; in the flat/graph record it carries no node.
_INERT_KINDS = {"transform"}

# Kinds that are valid firing entry points (initiators, §9.3.1). Several INDEPENDENT
# initiator block types (no "family" abstraction) — schedule Trigger + the new boundary
# sources, each fired by its own external emitter service.
_INITIATOR_KINDS = {"trigger", "file_initiator", "web_initiator", "stt_initiator"}
_DEST_KINDS = {"whatsapp", "tts", "bus", "file_destination", "web_destination"}


def _decompose_agent_capabilities(nodes, edges):
    """Split an Agent carrying RAG-pre and/or Guardrails config into explicit graph nodes so
    the node-based executor actually runs them:  …→[rag]→agent→[guardrail]→…

    The rag/guardrails config is MOVED off the agent record onto the new nodes (mirrors
    ``dsl_graph.from_flat_record``). Fan-in/out is preserved: EVERY edge into the agent is
    rerouted into its rag node, EVERY edge out of the agent leaves from its guardrail node
    (so an ``A→B`` between two capable agents becomes ``guardrail-A→rag-B``). RAG-pre is
    emitted only when ``rag.domains`` is non-empty (rewriter/use_graph alone don't retrieve).
    No-op — returns the inputs unchanged — when no agent has these capabilities."""
    from ..dsl_graph import GraphEdge, GraphNode

    rag_of: dict[str, str] = {}
    guard_of: dict[str, str] = {}
    for n in nodes:
        if n.kind != "agent":
            continue
        rec = (n.config or {}).get("record") or {}
        if (rec.get("rag") or {}).get("domains"):
            rag_of[n.id] = f"rag-{n.id}"
        if rec.get("guardrails"):
            guard_of[n.id] = f"guardrail-{n.id}"
    if not rag_of and not guard_of:
        return nodes, edges

    new_nodes: list = []
    for n in nodes:
        if n.kind == "agent" and (n.id in rag_of or n.id in guard_of):
            rec = dict((n.config or {}).get("record") or {})
            if n.id in rag_of:
                new_nodes.append(GraphNode(id=rag_of[n.id], kind="rag",
                                           config={"rag": rec["rag"]}))
                rec = {k: v for k, v in rec.items() if k != "rag"}
            if n.id in guard_of:
                new_nodes.append(GraphNode(id=guard_of[n.id], kind="guardrail",
                                           config={"guardrails": rec["guardrails"]}))
                rec = {k: v for k, v in rec.items() if k != "guardrails"}
            new_nodes.append(n.model_copy(update={"config": {**(n.config or {}), "record": rec}}))
        else:
            new_nodes.append(n)

    new_edges: list = []
    for e in edges:
        src = guard_of.get(e.src, e.src)   # agent's OUTgoing now leaves the guardrail node
        dst = rag_of.get(e.dst, e.dst)     # agent's INcoming now enters the rag node
        new_edges.append(e.model_copy(update={"src": src, "dst": dst}))
    for aid, rid in rag_of.items():
        new_edges.append(GraphEdge(src=rid, dst=aid))       # rag → agent
    for aid, gid in guard_of.items():
        new_edges.append(GraphEdge(src=aid, dst=gid))       # agent → guardrail

    return new_nodes, new_edges


class ProjectLowering:
    """Lower a Patron Project composition into a ``GraphRecord`` + advisory warnings.

    Kept as a class so the intermediate maps (node id -> block, node id -> stable
    graph node id) are shared by the record build and the warning computation.
    """

    def __init__(self, uid: str, name: str, composition: dict[str, Any]) -> None:
        self._uid = uid
        self._name = name
        self._graph = Graph(composition or {})
        # Stable per-composition node id: "<type>:<litegraph id>" (unique + readable),
        # matching the IR node_key convention so a GraphRecord node id is traceable
        # back to its canvas block.
        self._gid: dict[Any, str] = {}
        for n in self._graph.nodes:
            self._gid[n.get("id")] = f"{n.get('type')}:{n.get('id')}"

    # ---- helpers ----
    def _initiators(self) -> list[dict[str, Any]]:
        return [n for n in self._graph.nodes if n.get("type") in _INITIATOR_KINDS]

    def _asset_ref(self, node: dict[str, Any]) -> Optional[str]:
        """The bound asset id for a node (§9.2): the pointer to what fills the slot."""
        kind = node.get("type")
        props = Graph._props(node)
        if kind in _INITIATOR_KINDS:
            # The initiator binds the source that fires this Project; its firing IS the
            # Project's firing (§9.3.1). The composition carries the intended source id
            # under agent_id — kept as the firing asset ref (schedule / watch / route).
            return props.get("agent_id") or None
        if kind == "agent":
            return props.get("persona") or None
        if kind in _DEST_KINDS:
            return props.get("target") or None
        return None

    # ---- the lowering ----
    def build(self) -> tuple["GraphRecordT", list[str]]:
        from ..dsl_graph import GraphEdge, GraphNode, GraphRecord

        warnings = self.warnings()  # computed on the raw composition (before build)

        nodes: list[Any] = []
        edges: list[Any] = []
        for n in self._graph.nodes:
            comp_kind = n.get("type")
            node_kind = _KIND_MAP.get(comp_kind)
            if node_kind is None:
                # Unknown block type: skip it (warned already) rather than crash.
                continue
            block = self._graph._block(n)
            config: dict[str, Any] = {}
            if block is not None:
                frag = block.lower()
                if comp_kind == "agent":
                    config = {"record": self._graph._agent_record(n, frag)}
                elif comp_kind in _INITIATOR_KINDS:
                    config = dict(frag.get("trigger", {}))
                elif comp_kind in _DEST_KINDS:
                    config = dict(frag.get("delivery", {}))
                else:
                    config = dict(frag)
            nodes.append(
                GraphNode(
                    id=self._gid[n.get("id")],
                    kind=node_kind,  # type: ignore[arg-type]
                    asset_ref=self._asset_ref(n),
                    config=config,
                )
            )

        gid_set = set(self._gid.values())
        known_kind_ids = {self._gid[n.get("id")] for n in self._graph.nodes
                          if _KIND_MAP.get(n.get("type")) is not None}
        for lk in self._graph.links:
            if len(lk) < 4:
                continue
            src = self._gid.get(lk[1])
            dst = self._gid.get(lk[3])
            # Only keep edges whose BOTH ends became real nodes (skipped unknown blocks
            # drop their edges — warned, never crash).
            if src in known_kind_ids and dst in known_kind_ids:
                port = self._graph._out_slot_label(self._graph._node(lk[1]), lk[2])
                edges.append(GraphEdge(src=src, dst=dst, port=port))

        # Decompose agent-embedded capabilities into their graph nodes (§8.1): an Agent
        # carrying RAG-pre and/or Guardrails config becomes  …→[rag]→agent→[guardrail]→…
        # so the executor's h_rag / h_guardrail handlers actually run them. Without this the
        # config rides along on the agent record but is never applied (the graph executor is
        # node-based; h_agent only runs the brain). Mirrors from_flat_record's decomposition,
        # generalized to an arbitrary graph (fan-in/out preserved).
        nodes, edges = _decompose_agent_capabilities(nodes, edges)

        # A record needs >=1 node. If the composition has NO lowerable blocks at all, we
        # cannot build a record; this is the ONE hard failure (§9.3) — the caller turns it
        # into a 422. (An unsupported/inert-only composition also lands here.)
        if not nodes:
            raise LoweringError(
                "composition has no lowerable blocks — nothing to deploy"
            )

        # Pick the entry (§9.3.1): the single initiator if there is exactly one; else the
        # first initiator if several. With NO initiator, let the GraphRecord derive it (the
        # single node with no incoming edge). If derivation would ALSO fail (no initiator +
        # multiple roots), we still must NOT crash (advisory-only) — fall back to the first
        # lowered node as the entry so the record validates. The "no initiator" /
        # "multiple roots" condition is already surfaced as a warning (see ``warnings()``).
        entry: Optional[str] = None
        node_ids = [n.id for n in nodes]
        inits = self._initiators()
        init_ids = [self._gid[i.get("id")] for i in inits if self._gid[i.get("id")] in node_ids]
        if init_ids:
            entry = init_ids[0]
        else:
            has_incoming = {e.dst for e in edges}
            roots = [nid for nid in node_ids if nid not in has_incoming]
            # Exactly one root -> the GraphRecord will derive it; leave entry=None.
            # Zero or multiple roots -> derivation would raise, so pin the entry to the
            # first node (warned, never crash).
            entry = None if len(roots) == 1 else node_ids[0]

        record = GraphRecord(
            version=DSL_VERSION,
            uid=self._uid,
            name=self._name,
            enabled=True,
            entry=entry,
            nodes=nodes,
            edges=edges,
        )
        return record, warnings

    # ---- advisory validation (§9.3): warn, never refuse ----
    def warnings(self) -> list[str]:
        w: list[str] = []
        g = self._graph
        nodes = g.nodes

        # 1) no initiator -> the Project can never fire (§9.3).
        inits = self._initiators()
        if not inits:
            w.append(
                "no initiator: this composition has no Trigger, so it can never fire"
            )
        elif len(inits) > 1:
            # Multiple initiators are legal (§7.2) but worth flagging on a Trigger deploy.
            w.append(
                f"{len(inits)} initiators present; the firing binding is created for the "
                "first schedule-type Trigger only"
            )

        # 1b) no single entry root: with no initiator to pin the entry, a composition whose
        # lowerable nodes have more than one (or zero) roots cannot derive a single entry
        # (§9.3.1). This is advisory — deploy pins a fallback entry and warns, never refuses.
        if not inits:
            lowerable = [n for n in nodes if _KIND_MAP.get(n.get("type")) is not None]
            has_incoming = {lk[3] for lk in g.links if len(lk) >= 4}
            roots = [n for n in lowerable if n.get("id") not in has_incoming]
            if len(roots) > 1:
                labels = sorted(f"{n.get('type')}:{n.get('id')}" for n in roots)
                w.append(
                    f"multiple roots and no initiator: {labels} have no incoming edge, so no "
                    "single entry point can be derived (a fallback entry is used)"
                )

        # 2) unknown / unbound / missing-required-config blocks.
        has_incoming = {lk[3] for lk in g.links if len(lk) >= 4}
        has_outgoing = {lk[1] for lk in g.links if len(lk) >= 4}
        for n in nodes:
            nid = n.get("id")
            kind = n.get("type")
            label = f"{kind}:{nid}"
            if _KIND_MAP.get(kind) is None:
                if kind in _INERT_KINDS:
                    w.append(
                        f"unsupported block type '{kind}' ({label}) — it produces no runtime "
                        "node and will be skipped"
                    )
                else:
                    w.append(f"unknown block type '{kind}' ({label}) — it will be dropped")
                continue
            block = g._block(n)
            if block is not None:
                for err in block.validate():
                    w.append(f"missing/invalid config on {label}: {err}")
            # unbound: a block with no asset binding where one is expected.
            if kind in ("agent",) and not self._asset_ref(n):
                w.append(f"unbound block {label}: no persona/asset selected")
            if kind in _DEST_KINDS and not self._asset_ref(n):
                w.append(f"unbound block {label}: no destination target selected")
            # a non-initiator, non-destination block wired to nothing on either side.
            if (
                kind not in _INITIATOR_KINDS
                and kind not in _DEST_KINDS
                and nid not in has_incoming
                and nid not in has_outgoing
            ):
                w.append(f"unwired block {label}: not connected to anything")

        # 3) type-incompatible edges: a link whose endpoints' declared port schemas
        # do not match (advisory — the runtime tolerates str->str; a real mismatch is
        # a design smell worth surfacing).
        w.extend(self._edge_type_warnings())
        return w

    def _edge_type_warnings(self) -> list[str]:
        """Flag links whose source out-port schema is incompatible with the target
        in-port schema. Uses each block's declared ``get_schema()`` ports."""
        out: list[str] = []
        for lk in self._graph.links:
            if len(lk) < 5:
                continue
            src_node = self._graph._node(lk[1])
            dst_node = self._graph._node(lk[3])
            if src_node is None or dst_node is None:
                continue
            src_block = self._graph._block(src_node)
            dst_block = self._graph._block(dst_node)
            if src_block is None or dst_block is None:
                continue
            src_outs = src_block.ports("out")
            dst_ins = dst_block.ports("in")
            try:
                src_schema = src_outs[int(lk[2])].schema
                dst_schema = dst_ins[int(lk[4])].schema
            except (IndexError, TypeError, ValueError, AttributeError):
                continue
            # Structural sub-typing (schema.DataSchema): the source out-port must be
            # able to feed the target in-port. ANY is a wildcard on either side.
            if not src_schema.is_compatible_with(dst_schema):
                out.append(
                    f"type-incompatible edge {src_node.get('type')}:{src_node.get('id')}"
                    f" -> {dst_node.get('type')}:{dst_node.get('id')}: "
                    f"{src_schema.type} is not assignable to {dst_schema.type}"
                )
        return out


# A lightweight alias for the return type without importing at module scope (avoids a
# circular import: dsl_graph does not import lower, so this is only a type hint aid).
GraphRecordT = Any


def lower_project(
    uid: str, name: str, composition: dict[str, Any]
) -> tuple["GraphRecordT", list[str]]:
    """Lower a Patron Project composition into one ``GraphRecord`` keyed by ``uid``,
    plus advisory warnings (§9.3). Raises ``LoweringError`` only if there is literally
    nothing to deploy (no lowerable blocks)."""
    return ProjectLowering(uid, name, composition).build()
