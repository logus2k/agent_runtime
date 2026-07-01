"""composer Phase 1: the regression gate.

The link-traced Python lowering MUST reproduce ``patron/js/compile.js``'s output on the
News Agent, byte-for-byte (design §4.5). If the generic model can't reproduce the one
case that already runs, it isn't done. We also prove the lowering is *link-traced* (not
presence-based like compile.js): a graph whose result chain reaches no destination
fails loudly, and the lowered record validates against the live ``dsl.py`` AgentRecord.
"""

import copy
import json
from pathlib import Path

import pytest

from agent_runtime.composer import lower_graph
from agent_runtime.composer.lower import Graph
from agent_runtime.dsl import AgentRecord

FIXTURES = Path(__file__).parent / "fixtures"
GRAPH = json.loads((FIXTURES / "news_agent.graph.json").read_text())
GOLDEN = json.loads((FIXTURES / "news_agent.golden.json").read_text())


def test_news_agent_lowers_identically_to_compile_js():
    """The whole point: composer lowering == compile.js golden."""
    result = lower_graph(copy.deepcopy(GRAPH))
    expected = {"ok": GOLDEN["ok"], "dsl": GOLDEN["dsl"], "schedule": GOLDEN["schedule"]}
    assert result == expected


def test_lowered_record_validates_against_runtime_dsl():
    """The lowered flat record must load under the live AgentRecord. compile.js emits
    'id' (the admin layer maps it to uid+name on deploy); we complete those two fields
    the way the deploy bridge does, then assert the rest validates."""
    dsl = dict(lower_graph(copy.deepcopy(GRAPH))["dsl"])
    name = dsl.pop("id")
    rec = AgentRecord.model_validate(
        {**dsl, "uid": "00000000-0000-4000-8000-000000000000", "name": name}
    )
    assert rec.brain.persona == "news_curator"
    assert rec.delivery.channel == "whatsapp"
    assert rec.tools.allow == ["mcp__newsapi_search", "mcp__fetch_url"]


def test_lowering_is_link_traced_not_presence_based():
    """Remove the wire from Deliver to WhatsApp: compile.js (presence) would still
    emit; the link-traced lowering must fail loudly because the result chain reaches
    no destination."""
    broken = copy.deepcopy(GRAPH)
    # links: [..., [4, 4, 0, 5, 0, "destination"]] is Deliver(4) -> WhatsApp(5).
    broken["links"] = [lk for lk in broken["links"] if not (lk[1] == 4 and lk[3] == 5)]
    result = lower_graph(broken)
    assert result["ok"] is False
    assert any("destination" in e for e in result["errors"])


def test_missing_trigger_fails_loudly():
    broken = copy.deepcopy(GRAPH)
    broken["nodes"] = [n for n in broken["nodes"] if n["type"] != "patron/agent/trigger"]
    result = lower_graph(broken)
    assert result["ok"] is False
    assert any("Trigger" in e for e in result["errors"])


def test_non_dict_graph_raises():
    from agent_runtime.composer.lower import LoweringError

    with pytest.raises(LoweringError):
        Graph(["not", "a", "graph"])  # type: ignore[arg-type]


def test_schedule_defaults_match_compile_js():
    """The fixture's Trigger carries no cron/timezone props; compile.js defaults them
    to '0 7 * * *' and '' (UTC). The lowering must do the same."""
    sched = lower_graph(copy.deepcopy(GRAPH))["schedule"]
    assert sched == {"cron": "0 7 * * *", "timezone": ""}
