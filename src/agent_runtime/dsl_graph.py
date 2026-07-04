"""The graph/workflow runtime record — the graph form of a deployed Project (§9.3).

Where ``dsl.py``'s ``AgentRecord`` is the **flat single-agent** record (one Brain, one
Delivery), a ``GraphRecord`` is the **graph/workflow** record the spec calls for: one
persisted ``{ uid, version, nodes[], edges[] }`` where each node references its bound
asset id (§9.2) and typed edges are the glue between them (§9.3). One Project deploys
1:1 to one ``GraphRecord``; re-deploy is idempotent and bumps ``version`` (§9.3).

Decomposition (§8.1): the monolithic ``AgentRecord`` is split into node types —

  * **initiator** — the entry boundary (schedule/channel/file/web). Carries the bound
    schedule/watch/route asset ref; its firing IS the workflow's firing (§9.3.1).
  * **rag** — pre-inference retrieve-then-inject, wired BEFORE an agent (§8.1).
  * **guardrail** — a check block wired before/after/both an agent (§8.1).
  * **agent** — the pure agent: persona + tools + (skills/loop later). Carries its own
    ``AgentRecord``-shaped config so the Brain node reuses ``run_brain`` verbatim.
  * **destination** — a delivery sink (whatsapp/bus/tts).

Validation is strict and loud (``extra="forbid"``, unique node ids, edges reference
real nodes, exactly one entry that is a real node) — the same ethos as ``dsl.py``.

The **compat shim** (``from_flat_record``) loads a legacy flat ``AgentRecord`` as a
degenerate graph (initiator → [rag?] → [guardrail-in?] → agent → [guardrail-out?] →
destination) so the existing News Agent keeps running unchanged (§9.3, Phase 04).
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .dsl import AgentRecord, SUPPORTED_MAJOR

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)$")

# The node kinds the graph executor understands. ``initiator`` is the entry boundary;
# ``destination`` is a channel sink whose channel is carried in config. The Phase-08a
# source blocks (File/Web/STT initiators) all lower to ``initiator`` and the sink blocks
# (File/Web destinations) all lower to ``destination`` — several block TYPES, one node
# KIND — so this Literal is unchanged: the specific channel/source lives in node config.
NodeKind = Literal[
    "initiator", "rag", "vector_query", "graph_query", "guardrail", "agent", "destination",
    "data",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GraphNode(_Strict):
    """One node in the workflow graph.

    ``id`` is unique within the record (the slot). ``kind`` selects the handler.
    ``asset_ref`` is a pointer to the bound asset id (§9.2) — the schedule id for an
    initiator, the persona/preset id for an agent, the destination target for a
    destination — distinct from the node id (the slot vs. what fills it). ``config``
    holds the node's own parameters (e.g. an agent node's ``AgentRecord``-shaped dict,
    a destination's channel + target, a guardrail's policy)."""

    id: str
    kind: NodeKind
    asset_ref: Optional[str] = None
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("node id must be a non-empty string")
        return v


class GraphEdge(_Strict):
    """A typed directed edge (the glue). ``port`` is the source out-port label —
    ``"out"`` for a plain flow, a branch/loop label otherwise."""

    src: str
    dst: str
    port: str = "out"


class GraphRecord(_Strict):
    """A deployed Project as one graph/workflow record (§9.3).

    Required: ``version``, ``uid``, ``name``, ``nodes``, ``edges``. ``enabled`` mirrors
    the flat record (the farm skips a disabled record). Exactly-one-entry is validated:
    the ``entry`` node id must be a real node; if omitted it is derived as the single
    node with no incoming edge (loud if ambiguous)."""

    version: str
    uid: str
    name: str
    description: Optional[str] = None
    enabled: bool = True
    # Multi-tenancy (documents/multi_tenancy.md §4): the owning principal (OIDC sub),
    # stamped at deploy. None = legacy record → treated as owned by the default principal.
    owner: Optional[str] = None
    owner_email: Optional[str] = None
    entry: Optional[str] = None
    nodes: list[GraphNode]
    edges: list[GraphEdge] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def _known_major(cls, v: str) -> str:
        m = _VERSION_RE.match(v)
        if not m:
            raise ValueError(f"version '{v}' must be 'major.minor' (e.g. '0.1')")
        major = int(m.group(1))
        if major != SUPPORTED_MAJOR:
            raise ValueError(
                f"unsupported DSL major version '{v}': this runtime supports "
                f"major {SUPPORTED_MAJOR}.x only"
            )
        return v

    @field_validator("name")
    @classmethod
    def _name_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("name must be a non-empty label")
        return v

    @model_validator(mode="after")
    def _validate_graph(self) -> "GraphRecord":
        if not self.nodes:
            raise ValueError("a graph record must have at least one node")
        ids = [n.id for n in self.nodes]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate node ids in graph record: {sorted(dupes)}")
        idset = set(ids)
        for e in self.edges:
            if e.src not in idset:
                raise ValueError(f"edge src '{e.src}' is not a node in this record")
            if e.dst not in idset:
                raise ValueError(f"edge dst '{e.dst}' is not a node in this record")
        # Resolve / validate the entry node.
        if self.entry is not None:
            if self.entry not in idset:
                raise ValueError(f"entry '{self.entry}' is not a node in this record")
        else:
            has_incoming = {e.dst for e in self.edges}
            roots = [i for i in ids if i not in has_incoming]
            if len(roots) != 1:
                raise ValueError(
                    "cannot derive a single entry node (nodes with no incoming edge: "
                    f"{sorted(roots)}); set 'entry' explicitly"
                )
            # Assign the derived entry (model is not frozen).
            object.__setattr__(self, "entry", roots[0])
        return self

    # ---- accessors used by the executor ----

    def node(self, node_id: str) -> GraphNode:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(f"no node '{node_id}' in graph record {self.uid}")

    def out_edges(self, node_id: str, port: Optional[str] = None) -> list[GraphEdge]:
        return [
            e for e in self.edges
            if e.src == node_id and (port is None or e.port == port)
        ]

    def successors(self, node_id: str, port: Optional[str] = None) -> list[str]:
        return [e.dst for e in self.out_edges(node_id, port)]

    def is_sink(self, node_id: str) -> bool:
        return not any(e.src == node_id for e in self.edges)


# The uid used for the synthetic AgentRecord embedded in a shim'd agent node. The real
# routing uid stays on the GraphRecord; this only exists to satisfy AgentRecord's shape.
_EMBED_UID = "00000000-0000-4000-8000-000000000000"


def from_flat_record(record: AgentRecord) -> GraphRecord:
    """The compat shim (§9.3, Phase 04): load a legacy flat ``AgentRecord`` as a
    degenerate graph so the existing News Agent keeps running unchanged.

    The decomposition (§8.1) is exercised: a ``rag`` node is emitted before the agent
    when the flat record carries a RAG-pre config; a ``guardrail`` node is emitted after
    the agent when the flat record carries guardrails. The chain is:

        initiator → [rag] → agent → [guardrail] → destination

    tools/skills/loop stay ON the agent node (its embedded record). The initiator's
    ``asset_ref`` is the flat record's uid (the schedule binds to it, §9.3.1); the agent
    node's ``asset_ref`` is the persona; the destination's is the delivery target."""
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []

    initiator_id = "initiator"
    agent_id = "agent"
    dest_id = record.delivery.channel  # e.g. "whatsapp" | "bus" | "tts"

    nodes.append(
        GraphNode(
            id=initiator_id,
            kind="initiator",
            asset_ref=record.uid,
            config={"type": record.trigger.type},
        )
    )

    # The agent node carries the whole flat record (minus trigger/delivery/rag/guardrails,
    # which are their own nodes) so run_brain reuses it verbatim. We keep the full record
    # under config["record"] but strip the decomposed concerns so tools/skills stay on the
    # agent and rag/guardrails are NOT double-applied inside the agent handler.
    embed = record.model_dump(mode="python")
    embed.pop("rag", None)
    embed.pop("guardrails", None)
    nodes.append(
        GraphNode(
            id=agent_id,
            kind="agent",
            asset_ref=record.brain.persona,
            config={"record": embed},
        )
    )

    prev = initiator_id
    # RAG-pre: its own node, wired BEFORE the agent (§8.1).
    if record.rag is not None:
        rag_id = "rag"
        nodes.append(
            GraphNode(id=rag_id, kind="rag", config={"rag": record.rag.model_dump()})
        )
        edges.append(GraphEdge(src=prev, dst=rag_id))
        prev = rag_id
    edges.append(GraphEdge(src=prev, dst=agent_id))
    prev = agent_id

    # Guardrail: its own node, wired AFTER the agent (output-side check, §8.1).
    if record.guardrails is not None:
        guard_id = "guardrail"
        nodes.append(
            GraphNode(
                id=guard_id,
                kind="guardrail",
                config={"guardrails": record.guardrails.model_dump()},
            )
        )
        edges.append(GraphEdge(src=prev, dst=guard_id))
        prev = guard_id

    nodes.append(
        GraphNode(
            id=dest_id,
            kind="destination",
            asset_ref=record.delivery.target,
            config={
                "channel": record.delivery.channel,
                "target": record.delivery.target,
                "target_name": record.delivery.target_name,
            },
        )
    )
    edges.append(GraphEdge(src=prev, dst=dest_id))

    return GraphRecord(
        version=record.version,
        uid=record.uid,
        name=record.name,
        description=record.description,
        enabled=record.enabled,
        entry=initiator_id,
        nodes=nodes,
        edges=edges,
    )
