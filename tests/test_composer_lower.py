"""composer lowering — FIXTURE-DRIVEN.

Every test parametrizes over the discovered (graph, golden) fixture pairs and derives all
concrete values from them. No agent value (persona, target, schedule, tools…) is embedded
in a test body, so these run unchanged against any agent added under tests/fixtures/.
"""

import copy

import pytest

from agent_runtime.composer import lower_graph
from agent_runtime.composer.lower import Graph, LoweringError
from agent_runtime.dsl import AgentRecord

from _agentfixtures import agent_fixture_params

PAIRS = agent_fixture_params()


@pytest.mark.parametrize("graph, golden", PAIRS)
def test_graph_lowers_to_golden(graph, golden):
    """The contract: a graph lowers exactly to its golden {ok, dsl, schedule}."""
    assert lower_graph(copy.deepcopy(graph)) == {
        "ok": golden["ok"], "dsl": golden["dsl"], "schedule": golden["schedule"],
    }


@pytest.mark.parametrize("graph, golden", PAIRS)
def test_lowered_record_validates_against_runtime_dsl(graph, golden):
    """The lowered record loads under the live AgentRecord once the admin-assigned
    uid/name are supplied (the deploy bridge derives them from the record's 'id')."""
    dsl = dict(lower_graph(copy.deepcopy(graph))["dsl"])
    name = dsl.pop("id")
    rec = AgentRecord.model_validate(
        {**dsl, "uid": "00000000-0000-4000-8000-000000000000", "name": name}
    )
    # The validated record must agree with the golden on the fields the golden declares.
    assert rec.brain.persona == golden["dsl"]["brain"]["persona"]
    assert rec.delivery.model_dump() == golden["dsl"]["delivery"]
    if "tools" in golden["dsl"]:
        assert rec.tools.allow == golden["dsl"]["tools"]["allow"]


@pytest.mark.parametrize("graph, golden", PAIRS)
def test_schedule_matches_golden(graph, golden):
    assert lower_graph(copy.deepcopy(graph))["schedule"] == golden["schedule"]


@pytest.mark.parametrize("graph, golden", PAIRS)
def test_lowering_is_link_traced_not_presence_based(graph, golden):
    """Cut the agent's outgoing wire(s): the link-traced lowering must fail loudly (a
    presence-based compiler would still emit). Generic — finds the agent node itself."""
    broken = copy.deepcopy(graph)
    agent_ids = {n["id"] for n in broken["nodes"] if n["type"] == "agent"}
    broken["links"] = [lk for lk in broken["links"] if lk[1] not in agent_ids]
    result = lower_graph(broken)
    assert result["ok"] is False
    assert any("destination" in e for e in result["errors"])


@pytest.mark.parametrize("graph, golden", PAIRS)
def test_missing_trigger_fails_loudly(graph, golden):
    broken = copy.deepcopy(graph)
    broken["nodes"] = [n for n in broken["nodes"] if n["type"] != "trigger"]
    result = lower_graph(broken)
    assert result["ok"] is False
    assert any("Trigger" in e for e in result["errors"])


def test_non_dict_graph_raises():
    with pytest.raises(LoweringError):
        Graph(["not", "a", "graph"])  # type: ignore[arg-type]
