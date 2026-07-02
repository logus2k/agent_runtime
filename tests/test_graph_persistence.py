"""Graph-registry persistence (§9.3): a Deploy survives a restart.

The registry writes one ``<uid>.json`` per record on upsert and removes it on delete;
a fresh registry over the same dir reloads everything. This is what stops a deployed
Project being lost on an ``agent-runtime-app`` reboot (which would strand the scheduler
firing-binding on an orphaned ``record_uid``).
"""
from __future__ import annotations

import pytest

from agent_runtime.dsl_graph import GraphEdge, GraphNode, GraphRecord
from agent_runtime.graph_registry import GraphRegistry


def _rec(uid: str = "p1", name: str = "News Agent", version: str = "0.1") -> GraphRecord:
    return GraphRecord(
        version=version, uid=uid, name=name,
        nodes=[
            GraphNode(id="init", kind="initiator"),
            GraphNode(id="a", kind="agent", config={"record": {}}),
            GraphNode(id="d1", kind="destination", config={"channel": "bus", "target": "one"}),
            GraphNode(id="d2", kind="destination", config={"channel": "bus", "target": "two"}),
        ],
        # fan-out edge included so persistence is exercised on a many-to-many graph too
        edges=[GraphEdge(src="init", dst="a"),
               GraphEdge(src="a", dst="d1"), GraphEdge(src="a", dst="d2")],
    )


def test_upsert_writes_a_file_and_reload_sees_it(tmp_path):
    reg = GraphRegistry(tmp_path)
    reg.upsert(_rec())
    assert (tmp_path / "p1.json").exists()

    # Simulate a restart: a brand-new registry over the same dir.
    reg2 = GraphRegistry(tmp_path)
    got = reg2.require("p1")
    assert got.uid == "p1"
    assert got.name == "News Agent"
    # fan-out survived the round-trip (2 edges out of the agent)
    assert len({(e.src, e.dst) for e in got.edges}) == 3
    assert sum(1 for e in got.edges if e.src == "a") == 2


def test_delete_removes_the_file(tmp_path):
    reg = GraphRegistry(tmp_path)
    reg.upsert(_rec())
    assert reg.delete("p1") is True
    assert not (tmp_path / "p1.json").exists()
    # A fresh registry no longer sees it.
    assert GraphRegistry(tmp_path).get("p1") is None


def test_version_bump_persists_across_reload(tmp_path):
    reg = GraphRegistry(tmp_path)
    reg.upsert(_rec(version="0.1"))          # first insert keeps 0.1
    stored = reg.upsert(_rec(version="0.1"))  # idempotent re-deploy bumps -> 0.2
    assert stored.version == "0.2"
    # Reload: the bumped version is what's on disk.
    assert GraphRegistry(tmp_path).require("p1").version == "0.2"


def test_corrupt_record_file_raises_loudly(tmp_path):
    (tmp_path / "bad.json").write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(ValueError):
        GraphRegistry(tmp_path)


def test_no_store_dir_is_pure_memory(tmp_path):
    # store_dir=None -> nothing written; used by the existing unit tests.
    reg = GraphRegistry()
    reg.upsert(_rec())
    assert list(tmp_path.iterdir()) == []
    assert reg.get("p1") is not None
