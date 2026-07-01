"""composer: lowering the News Agent (authored in the NEW vocabulary, no adapter).

lower_graph(news_agent.graph.json) must equal the golden runtime DSL, the lowered record
must validate against the live ``dsl.py`` AgentRecord, and the lowering must be
*link-traced* (a graph whose flow reaches no destination fails loudly).
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


def test_news_agent_lowers_to_golden_runtime_dsl():
    """The whole point: the new-vocabulary graph lowers to the expected runtime DSL."""
    result = lower_graph(copy.deepcopy(GRAPH))
    expected = {"ok": GOLDEN["ok"], "dsl": GOLDEN["dsl"], "schedule": GOLDEN["schedule"]}
    assert result == expected


def test_lowered_record_validates_against_runtime_dsl():
    """The lowered flat record must load under the live AgentRecord. The record carries
    'id'; the admin layer maps it to uid+name on deploy — we complete those two fields
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
    """Remove the Agent→WhatsApp wire: a presence-based compiler would still emit; the
    link-traced lowering must fail loudly because the flow reaches no destination."""
    broken = copy.deepcopy(GRAPH)
    # links: [2, 2, 0, 3, 0, "flow"] is Agent(2) -> WhatsApp(3).
    broken["links"] = [lk for lk in broken["links"] if not (lk[1] == 2 and lk[3] == 3)]
    result = lower_graph(broken)
    assert result["ok"] is False
    assert any("destination" in e for e in result["errors"])


def test_missing_trigger_fails_loudly():
    broken = copy.deepcopy(GRAPH)
    broken["nodes"] = [n for n in broken["nodes"] if n["type"] != "trigger"]
    result = lower_graph(broken)
    assert result["ok"] is False
    assert any("Trigger" in e for e in result["errors"])


def test_non_dict_graph_raises():
    from agent_runtime.composer.lower import LoweringError

    with pytest.raises(LoweringError):
        Graph(["not", "a", "graph"])  # type: ignore[arg-type]


def test_schedule_lowers_from_trigger_config():
    """The Trigger's cron/timezone config lowers to the scheduler-job spec beside the
    record (empty timezone == UTC)."""
    sched = lower_graph(copy.deepcopy(GRAPH))["schedule"]
    assert sched == {"cron": "0 7 * * *", "timezone": ""}
