"""Step 1: DSL model + loader — happy path and (loud) failure paths."""

import textwrap
from pathlib import Path

import pytest

from agent_runtime.dsl import AgentRecord
from agent_runtime.registry import Registry, RecordLoadError, load_record

REPO = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


# --- happy path -------------------------------------------------------------

def test_news_record_loads_from_repo():
    """The shipped News Agent record validates."""
    rec = load_record(REPO / "data" / "agents" / "news-morning-ai.yaml")
    assert rec.id == "news-morning-ai"
    assert rec.version == "0.1"
    assert rec.brain.persona == "news_curator"
    assert rec.tools is not None
    assert rec.tools.server == "mcp"
    assert rec.tools.allow == ["mcp__newsapi_search", "mcp__fetch_url"]
    assert rec.delivery.channel == "whatsapp"
    assert rec.input.vars["topic"] == "AI agents"
    # omitted optional stages are None, not silently defaulted
    assert rec.rag is None and rec.guardrails is None


def test_minimal_record_defaults():
    rec = AgentRecord.model_validate(
        {
            "version": "0.1",
            "id": "x",
            "brain": {"persona": "p"},
            "delivery": {"channel": "bus", "target": "stream:x"},
        }
    )
    assert rec.trigger.type == "schedule"  # default
    assert rec.memory.policy == "none"
    assert rec.tools is None


# --- failure paths (must raise, never silently pass) ------------------------

def test_unknown_field_rejected():
    with pytest.raises(Exception) as ei:
        AgentRecord.model_validate(
            {
                "version": "0.1",
                "id": "x",
                "brain": {"persona": "p"},
                "delivery": {"channel": "bus", "target": "t"},
                "tool": {"server": "noted"},  # typo: should be "tools"
            }
        )
    assert "tool" in str(ei.value)


def test_unknown_major_rejected():
    with pytest.raises(Exception) as ei:
        AgentRecord.model_validate(
            {
                "version": "1.0",
                "id": "x",
                "brain": {"persona": "p"},
                "delivery": {"channel": "bus", "target": "t"},
            }
        )
    assert "major" in str(ei.value).lower()


def test_bad_id_rejected():
    with pytest.raises(Exception):
        AgentRecord.model_validate(
            {
                "version": "0.1",
                "id": "bad id with spaces",
                "brain": {"persona": "p"},
                "delivery": {"channel": "bus", "target": "t"},
            }
        )


def test_malformed_tool_name_rejected():
    with pytest.raises(Exception) as ei:
        AgentRecord.model_validate(
            {
                "version": "0.1",
                "id": "x",
                "brain": {"persona": "p"},
                "tools": {"server": "noted", "allow": ["not_namespaced"]},
                "delivery": {"channel": "bus", "target": "t"},
            }
        )
    assert "server__tool" in str(ei.value) or "<server>__<tool>" in str(ei.value)


def test_loader_names_offending_file(tmp_path):
    p = _write(tmp_path, "broken.yaml", "version: '0.1'\nid: x\n")  # missing brain+delivery
    with pytest.raises(RecordLoadError) as ei:
        load_record(p)
    assert "broken.yaml" in str(ei.value)


def test_registry_rejects_duplicate_ids(tmp_path):
    body = """
    version: "0.1"
    id: dup
    brain: { persona: p }
    delivery: { channel: bus, target: t }
    """
    _write(tmp_path, "a.yaml", body)
    _write(tmp_path, "b.yaml", body)
    with pytest.raises(RecordLoadError) as ei:
        Registry(tmp_path).load_all()
    assert "duplicate" in str(ei.value).lower()


def test_registry_loads_repo_agents():
    reg = Registry(REPO / "data" / "agents")
    records = reg.load_all()
    assert "news-morning-ai" in records
    assert reg.require("news-morning-ai").brain.persona == "news_curator"
