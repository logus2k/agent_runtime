"""Phase 05 — the pure Project→GraphRecord lowering (``composer.lower.lower_project``).

Unit-level (no app, no scheduler): the composition (litegraph serialize shape) becomes
one ``GraphRecord`` in the graph form — agents/rag/guardrails/destinations as nodes, the
initiator as entry, links as typed edges — and advisory warnings are computed without
ever refusing. Fan-out (§7.2), multiple initiators, and type-incompatible edges (§9.3)
are exercised here rather than through the HTTP surface.
"""

from __future__ import annotations

import pytest

from agent_runtime.composer.lower import LoweringError, lower_project


def _trigger():
    return {
        "id": 1, "type": "trigger",
        "properties": {"agent_id": "x", "trigger_type": "schedule", "cron": "0 7 * * *"},
        "outputs": [{"name": "out", "links": [1]}],
    }


def test_linear_trigger_agent_whatsapp():
    comp = {
        "nodes": [
            _trigger(),
            {"id": 2, "type": "agent", "properties": {"persona": "p"},
             "inputs": [{"name": "in", "link": 1}], "outputs": [{"name": "out", "links": [2]}]},
            {"id": 3, "type": "whatsapp", "properties": {"target": "a@g.us"},
             "inputs": [{"name": "in", "link": 2}]},
        ],
        "links": [[1, 1, 0, 2, 0, "string"], [2, 2, 0, 3, 0, "string"]],
    }
    rec, warnings = lower_project("u1", "Linear", comp)
    assert rec.uid == "u1" and rec.entry == "trigger:1"
    assert {n.kind for n in rec.nodes} == {"initiator", "agent", "destination"}
    assert len(rec.edges) == 2
    assert warnings == []


def test_data_block_on_vars_port_folds_into_agent_input_vars():
    """An inline Data (JSON) block wired to an Agent's `vars` port (input slot 1) is folded
    into the Agent's input.vars at compile time (data overrides input_vars defaults), and the
    Data node is removed from the record (§7.1)."""
    comp = {
        "nodes": [
            _trigger(),
            {"id": 2, "type": "agent",
             "properties": {"persona": "p", "input_vars": {"n": 5}},
             "inputs": [{"name": "in", "link": 1}, {"name": "vars", "link": 3}],
             "outputs": [{"name": "out", "links": [2]}]},
            {"id": 3, "type": "bus", "properties": {"target": "b"},
             "inputs": [{"name": "in", "link": 2}]},
            {"id": 4, "type": "data", "properties": {"content": {"topic": "AI", "n": 9}},
             "outputs": [{"name": "out", "links": [3]}]},
        ],
        "links": [[1, 1, 0, 2, 0, "string"],
                  [2, 2, 0, 3, 0, "string"],
                  [3, 4, 0, 2, 1, "any"]],  # data(4).out -> agent(2).vars (input slot 1)
    }
    rec, warnings = lower_project("u", "D", comp)
    assert "data" not in {n.kind for n in rec.nodes}  # folded out (compile-time, no runtime node)
    agent = next(n for n in rec.nodes if n.kind == "agent")
    assert agent.config["record"]["input"]["vars"] == {"n": 9, "topic": "AI"}  # data > default n:5


def test_data_block_content_as_json_string_folds_too():
    """The Data block's content may arrive as a JSON STRING (Patron's json editor) — still folds."""
    comp = {
        "nodes": [
            _trigger(),
            {"id": 2, "type": "agent", "properties": {"persona": "p"},
             "inputs": [{"name": "in", "link": 1}, {"name": "vars", "link": 3}],
             "outputs": [{"name": "out", "links": [2]}]},
            {"id": 3, "type": "bus", "properties": {"target": "b"},
             "inputs": [{"name": "in", "link": 2}]},
            {"id": 4, "type": "data", "properties": {"content": '{"topic": "safety"}'},
             "outputs": [{"name": "out", "links": [3]}]},
        ],
        "links": [[1, 1, 0, 2, 0, "string"], [2, 2, 0, 3, 0, "string"], [3, 4, 0, 2, 1, "any"]],
    }
    rec, _ = lower_project("u", "D", comp)
    agent = next(n for n in rec.nodes if n.kind == "agent")
    assert agent.config["record"]["input"]["vars"] == {"topic": "safety"}


def test_data_block_wired_to_a_normal_in_stays_as_a_flow_node():
    """A Data block NOT on a `vars` port (here → an Agent's task `in`) is NOT folded — it stays
    as a runtime `data` node (the general flow-source path, h_data)."""
    comp = {
        "nodes": [
            {"id": 2, "type": "agent", "properties": {"persona": "p"},
             "inputs": [{"name": "in", "link": 3}], "outputs": [{"name": "out", "links": [2]}]},
            {"id": 3, "type": "bus", "properties": {"target": "b"},
             "inputs": [{"name": "in", "link": 2}]},
            {"id": 4, "type": "data", "properties": {"content": {"x": 1}},
             "outputs": [{"name": "out", "links": [3]}]},
        ],
        "links": [[3, 4, 0, 2, 0, "any"], [2, 2, 0, 3, 0, "string"]],  # data(4)->agent(2).in (slot 0)
    }
    rec, _ = lower_project("u", "D", comp)
    assert "data" in {n.kind for n in rec.nodes}  # kept — it's on the flow path, not folded


def test_file_source_data_on_vars_port_stays_as_a_node_with_a_vars_edge():
    """A FILE-source Data block wired to an Agent's `vars` port is NOT folded (its content is
    only known at runtime) — it stays as a `data` node, and the edge into the agent is tagged
    dst_port='vars' so the executor treats it as a PULL input (not a triggering message)."""
    comp = {
        "nodes": [
            _trigger(),
            {"id": 2, "type": "agent", "properties": {"persona": "p"},
             "inputs": [{"name": "in", "link": 1}, {"name": "vars", "link": 3}],
             "outputs": [{"name": "out", "links": [2]}]},
            {"id": 3, "type": "bus", "properties": {"target": "b"},
             "inputs": [{"name": "in", "link": 2}]},
            {"id": 4, "type": "data",
             "properties": {"source": "file", "path": "/watched/in/params.json"},
             "outputs": [{"name": "out", "links": [3]}]},
        ],
        "links": [[1, 1, 0, 2, 0, "string"],
                  [2, 2, 0, 3, 0, "string"],
                  [3, 4, 0, 2, 1, "any"]],  # data(4).out -> agent(2).vars (input slot 1)
    }
    rec, _ = lower_project("u", "D", comp)
    assert "data" in {n.kind for n in rec.nodes}  # NOT folded (file source, runtime pull)
    data = next(n for n in rec.nodes if n.kind == "data")
    agent = next(n for n in rec.nodes if n.kind == "agent")
    vars_edges = rec.in_edges(agent.id, dst_port="vars")
    assert [e.src for e in vars_edges] == [data.id]
    assert data.config == {"source": "file", "path": "/watched/in/params.json"}


def test_agent_with_rag_pre_decomposes_into_a_rag_node_before_the_agent():
    """An Agent carrying RAG-pre config (rag_domains) must lower to a `rag` node wired
    BEFORE the agent (initiator→rag→agent→dest), so the executor's h_rag runs it. §8.1."""
    comp = {
        "nodes": [
            _trigger(),
            {"id": 2, "type": "agent", "properties": {"persona": "p", "rag_domains": "cv"},
             "inputs": [{"name": "in", "link": 1}], "outputs": [{"name": "out", "links": [2]}]},
            {"id": 3, "type": "bus", "properties": {"target": "b"},
             "inputs": [{"name": "in", "link": 2}]},
        ],
        "links": [[1, 1, 0, 2, 0, "string"], [2, 2, 0, 3, 0, "string"]],
    }
    rec, _ = lower_project("u-rag", "Rag", comp)
    assert "rag" in {n.kind for n in rec.nodes}
    rag = next(n for n in rec.nodes if n.kind == "rag")
    assert rag.config["rag"]["domains"] == ["cv"]
    # wired initiator -> rag -> agent (rag precedes the agent)
    assert rec.successors("trigger:1") == [rag.id]
    assert rec.successors(rag.id) == ["agent:2"]
    # config was MOVED off the agent record (not left to ride along unused)
    agent = next(n for n in rec.nodes if n.kind == "agent")
    assert "rag" not in agent.config["record"]


def test_agent_with_guardrails_decomposes_into_a_guardrail_node_after_the_agent():
    comp = {
        "nodes": [
            _trigger(),
            {"id": 2, "type": "agent",
             "properties": {"persona": "p", "guard_forbidden": "secret,password"},
             "inputs": [{"name": "in", "link": 1}], "outputs": [{"name": "out", "links": [2]}]},
            {"id": 3, "type": "bus", "properties": {"target": "b"},
             "inputs": [{"name": "in", "link": 2}]},
        ],
        "links": [[1, 1, 0, 2, 0, "string"], [2, 2, 0, 3, 0, "string"]],
    }
    rec, _ = lower_project("u-gd", "Gd", comp)
    gd = next(n for n in rec.nodes if n.kind == "guardrail")
    assert gd.config["guardrails"]["forbidden"] == ["secret", "password"]
    # agent -> guardrail -> destination (guardrail sits AFTER the agent, before the sink)
    assert rec.successors("agent:2") == [gd.id]
    assert rec.successors(gd.id) == ["bus:4".replace("4", "3")]  # bus:3


def test_rag_and_guardrail_preserve_fan_in_to_the_agent():
    """Two initiators fan into one Agent with RAG-pre: BOTH incoming edges must reroute to
    the rag node (fan-in preserved), and the agent runs once per arrival downstream."""
    comp = {
        "nodes": [
            {"id": 1, "type": "trigger",
             "properties": {"agent_id": "x", "trigger_type": "schedule", "cron": "0 7 * * *"},
             "outputs": [{"name": "out", "links": [1]}]},
            {"id": 4, "type": "trigger",
             "properties": {"agent_id": "y", "trigger_type": "schedule", "cron": "0 8 * * *"},
             "outputs": [{"name": "out", "links": [3]}]},
            {"id": 2, "type": "agent", "properties": {"persona": "p", "rag_domains": "cv"},
             "inputs": [{"name": "in", "link": 1}], "outputs": [{"name": "out", "links": [2]}]},
            {"id": 3, "type": "bus", "properties": {"target": "b"},
             "inputs": [{"name": "in", "link": 2}]},
        ],
        "links": [[1, 1, 0, 2, 0, "string"], [3, 4, 0, 2, 0, "string"], [2, 2, 0, 3, 0, "string"]],
    }
    rec, _ = lower_project("u-fanin", "FanIn", comp)
    rag = next(n for n in rec.nodes if n.kind == "rag")
    # both triggers now feed the RAG node (not the agent directly)
    assert sorted(rec.successors("trigger:1") + rec.successors("trigger:4")) == [rag.id, rag.id]
    assert rec.successors(rag.id) == ["agent:2"]


def test_vector_and_graph_database_blocks_lower_to_query_nodes():
    """Standalone Vector/Graph Database blocks lower to `vector_query`/`graph_query` nodes
    (data sources that emit results into the flow — NOT agent-coupled)."""
    comp = {
        "nodes": [
            _trigger(),
            {"id": 2, "type": "vector_query", "properties": {"domain": "cv", "top_k": 3},
             "inputs": [{"name": "in", "link": 1}], "outputs": [{"name": "out", "links": [2]}]},
            {"id": 3, "type": "graph_query", "properties": {"domain": "cv", "query": "fixed q"},
             "inputs": [{"name": "in", "link": 2}], "outputs": [{"name": "out", "links": [3]}]},
            {"id": 4, "type": "bus", "properties": {"target": "out"},
             "inputs": [{"name": "in", "link": 3}]},
        ],
        "links": [[1, 1, 0, 2, 0, "string"], [2, 2, 0, 3, 0, "string"], [3, 3, 0, 4, 0, "string"]],
    }
    rec, warnings = lower_project("u-db", "DB", comp)
    kinds = {n.kind for n in rec.nodes}
    assert "vector_query" in kinds and "graph_query" in kinds
    vq = next(n for n in rec.nodes if n.kind == "vector_query")
    gq = next(n for n in rec.nodes if n.kind == "graph_query")
    assert vq.config == {"domain": "cv", "top_k": 3}
    assert gq.config == {"domain": "cv", "query": "fixed q"}
    assert warnings == []
    # chained: initiator -> vector -> graph -> destination
    assert rec.successors("trigger:1") == [vq.id]
    assert rec.successors(vq.id) == [gq.id]


def test_fanout_agent_to_two_destinations_is_graph_form():
    comp = {
        "nodes": [
            _trigger(),
            {"id": 2, "type": "agent", "properties": {"persona": "p"},
             "inputs": [{"name": "in", "link": 1}], "outputs": [{"name": "out", "links": [2, 3]}]},
            {"id": 3, "type": "whatsapp", "properties": {"target": "a@g.us"},
             "inputs": [{"name": "in", "link": 2}]},
            {"id": 4, "type": "bus", "properties": {"target": "b"},
             "inputs": [{"name": "in", "link": 3}]},
        ],
        "links": [[1, 1, 0, 2, 0, "string"], [2, 2, 0, 3, 0, "string"], [3, 2, 0, 4, 0, "string"]],
    }
    rec, warnings = lower_project("u2", "FanOut", comp)
    # the agent fans out to BOTH destinations (many-to-many, §7.2).
    assert sorted(rec.successors("agent:2")) == ["bus:4", "whatsapp:3"]
    assert warnings == []


def test_multiple_initiators_warns_but_lowers():
    comp = {
        "nodes": [
            {"id": 1, "type": "trigger",
             "properties": {"agent_id": "x", "trigger_type": "schedule"},
             "outputs": [{"name": "out", "links": [1]}]},
            {"id": 5, "type": "trigger",
             "properties": {"agent_id": "y", "trigger_type": "schedule"},
             "outputs": [{"name": "out", "links": [3]}]},
            {"id": 2, "type": "agent", "properties": {"persona": "p"},
             "inputs": [{"name": "in", "link": 1}], "outputs": [{"name": "out", "links": [2]}]},
            {"id": 3, "type": "whatsapp", "properties": {"target": "a@g.us"},
             "inputs": [{"name": "in", "link": 2}]},
        ],
        "links": [[1, 1, 0, 2, 0, "string"], [3, 5, 0, 2, 0, "string"], [2, 2, 0, 3, 0, "string"]],
    }
    rec, warnings = lower_project("u3", "TwoInits", comp)
    # both initiators became nodes; the agent has fan-in from both (per-message, §9.3.2).
    inits = [n for n in rec.nodes if n.kind == "initiator"]
    assert len(inits) == 2
    assert any("initiators present" in w for w in warnings)


def test_empty_composition_raises_lowering_error():
    with pytest.raises(LoweringError):
        lower_project("u4", "Empty", {"nodes": [], "links": []})


def test_unbound_and_no_initiator_warn():
    comp = {
        "nodes": [
            {"id": 2, "type": "agent", "properties": {},  # no persona -> unbound
             "outputs": [{"name": "out", "links": [2]}]},
            {"id": 3, "type": "whatsapp", "properties": {},  # no target -> unbound
             "inputs": [{"name": "in", "link": 2}]},
        ],
        "links": [[2, 2, 0, 3, 0, "string"]],
    }
    rec, warnings = lower_project("u5", "Headless", comp)
    joined = " ".join(warnings)
    assert "no initiator" in joined
    assert "unbound" in joined.lower()
    # STILL a valid record (advisory-only): 2 nodes, 1 edge.
    assert len(rec.nodes) == 2 and len(rec.edges) == 1
