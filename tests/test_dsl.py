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

def test_records_load_cleanly(tmp_path):
    """A well-formed flat record validates and loads. Uses a fixture (NOT data/agents,
    which is admin-editable and — since the News Agent migrated to the graph form — may
    legitimately be empty)."""
    _write(tmp_path, "one.yaml", f"""
        version: "0.1"
        uid: {UID}
        name: news
        brain: {{ persona: news_curator }}
        delivery: {{ channel: whatsapp, target: "1@g.us" }}
    """)
    records = Registry(tmp_path).load_all()
    assert len(records) >= 1
    for rec in records.values():
        assert rec.version.startswith("0.")
        assert rec.name and rec.brain.persona
        assert rec.delivery.channel in ("whatsapp", "bus", "tts")


def test_repo_records_dir_all_valid_if_present():
    """Whatever records ship in data/agents (may be none post-migration) must all be
    valid — a broken shipped record is a real failure, an empty dir is fine."""
    records = Registry(REPO / "data" / "agents").load_all()
    for rec in records.values():
        assert rec.version.startswith("0.")
        assert rec.name and rec.brain.persona
        assert rec.enabled in (True, False)  # the field exists / defaults


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


def test_registry_keys_by_uid_and_name(tmp_path):
    _write(tmp_path, "one.yaml", f"""
        version: "0.1"
        uid: {UID}
        name: news
        brain: {{ persona: p }}
        delivery: {{ channel: bus, target: t }}
    """)
    reg = Registry(tmp_path)
    reg.load_all()
    rec = reg.all()[0]
    assert reg.get(rec.uid) is rec          # keyed by uid
    assert reg.get_by_name(rec.name) is rec  # secondary name index
