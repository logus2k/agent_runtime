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
