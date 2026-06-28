"""Step 1: DSL model + loader — happy path and (loud) failure paths.

Identity model: records carry an immutable ``uid`` (UUID) + an editable ``name``.
"""

import textwrap
from pathlib import Path

import pytest

from agent_runtime.dsl import AgentRecord
from agent_runtime.registry import Registry, RecordLoadError, load_record

REPO = Path(__file__).resolve().parents[1]
UID = "00000000-0000-4000-8000-000000000000"  # a valid UUID for fixtures


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def _base(**over) -> dict:
    rec = {
        "version": "0.1",
        "uid": UID,
        "name": "x",
        "brain": {"persona": "p"},
        "delivery": {"channel": "bus", "target": "t"},
    }
    rec.update(over)
    return rec


# --- happy path -------------------------------------------------------------

def test_news_record_loads_from_repo():
    """The shipped News Agent record validates and resolves by name."""
    reg = Registry(REPO / "data" / "agents")
    reg.load_all()
    rec = reg.get_by_name("news-morning-ai")
    assert rec is not None
    assert rec.name == "news-morning-ai"
    assert rec.version == "0.1"
    assert rec.brain.persona == "news_curator"
    assert rec.tools is not None
    assert rec.tools.allow == ["mcp__newsapi_search", "mcp__fetch_url"]
    assert rec.delivery.channel == "whatsapp"
    assert rec.input.vars["topic"] == "AI agents"
    assert rec.rag is None and rec.guardrails is None


def test_minimal_record_defaults():
    rec = AgentRecord.model_validate(_base())
    assert rec.trigger.type == "schedule"
    assert rec.memory.policy == "none"
    assert rec.tools is None


# --- failure paths (must raise, never silently pass) ------------------------

def test_unknown_field_rejected():
    with pytest.raises(Exception) as ei:
        AgentRecord.model_validate(_base(tool={"server": "noted"}))  # typo: tools
    assert "tool" in str(ei.value)


def test_unknown_major_rejected():
    with pytest.raises(Exception) as ei:
        AgentRecord.model_validate(_base(version="1.0"))
    assert "major" in str(ei.value).lower()


def test_bad_uid_rejected():
    with pytest.raises(Exception) as ei:
        AgentRecord.model_validate(_base(uid="not-a-uuid"))
    assert "uid" in str(ei.value).lower()


def test_empty_name_rejected():
    with pytest.raises(Exception) as ei:
        AgentRecord.model_validate(_base(name="   "))
    assert "name" in str(ei.value).lower()


def test_malformed_tool_name_rejected():
    with pytest.raises(Exception) as ei:
        AgentRecord.model_validate(
            _base(tools={"server": "noted", "allow": ["not_namespaced"]})
        )
    assert "server__tool" in str(ei.value) or "<server>__<tool>" in str(ei.value)


def test_loader_names_offending_file(tmp_path):
    p = _write(tmp_path, "broken.yaml", f"version: '0.1'\nuid: {UID}\nname: x\n")
    with pytest.raises(RecordLoadError) as ei:
        load_record(p)
    assert "broken.yaml" in str(ei.value)


def test_registry_rejects_duplicate_uids(tmp_path):
    body = f"""
    version: "0.1"
    uid: {UID}
    name: dup
    brain: {{ persona: p }}
    delivery: {{ channel: bus, target: t }}
    """
    _write(tmp_path, "a.yaml", body)
    _write(tmp_path, "b.yaml", body)
    with pytest.raises(RecordLoadError) as ei:
        Registry(tmp_path).load_all()
    assert "duplicate" in str(ei.value).lower()


def test_registry_loads_repo_agents():
    reg = Registry(REPO / "data" / "agents")
    reg.load_all()
    rec = reg.get_by_name("news-morning-ai")
    assert rec is not None and rec.brain.persona == "news_curator"
    assert reg.get(rec.uid) is rec  # keyed by uid
