"""Phase 08a — the new block kinds: File / Web / STT initiators (boundary SOURCES,
out-only, inert in graph execution) and File / Web destinations (SINKS).

Covers: the catalog advertises every new kind; each initiator lowers to the ``initiator``
graph-node kind and each destination to ``destination`` (no Phase-05 unsupported-kind
warning); a composition using them lowers to a GraphRecord; and the destinations deliver
through the executor with MOCKED IO (no real filesystem/HTTP).

All fakes/mocks — no live infra (no Valkey, no disk write, no HTTP).
"""

from __future__ import annotations

import dataclasses

import pytest
from agent_bus_client import new_event

from agent_runtime.composer import (
    Catalog,
    FileDestination,
    FileInitiator,
    SttInitiator,
    WebDestination,
    WebInitiator,
)
from agent_runtime.composer.lower import lower_project
from agent_runtime.config import Settings
from agent_runtime.dsl_graph import GraphEdge, GraphNode, GraphRecord
from agent_runtime.nodes.delivery import DeliveryError, deliver_file, deliver_web
from agent_runtime.runner import Runner


NEW_INITIATORS = [FileInitiator, WebInitiator, SttInitiator]
NEW_DESTINATIONS = [FileDestination, WebDestination]


# ============================ Catalog ==========================================
def test_catalog_exposes_every_new_kind():
    kinds = {e["type"] for e in Catalog().entries()}
    assert {
        "file_initiator", "web_initiator", "stt_initiator",
        "file_destination", "web_destination",
    } <= kinds


def test_new_initiators_are_out_only_sources():
    for cls in NEW_INITIATORS:
        ports = cls().get_schema().ports
        dirs = {p.direction for p in ports}
        assert dirs == {"out"}, f"{cls.__name__} must be out-only (a boundary source)"


def test_new_destinations_are_in_only_sinks():
    for cls in NEW_DESTINATIONS:
        ports = cls().get_schema().ports
        dirs = {p.direction for p in ports}
        assert dirs == {"in"}, f"{cls.__name__} must be in-only (a sink)"


def test_new_destinations_require_target():
    assert any("target" in e for e in FileDestination().validate())
    assert any("target" in e for e in WebDestination().validate())
    assert FileDestination(config={"target": "/tmp/out.txt"}).validate() == []
    assert WebDestination(config={"target": "https://x/y"}).validate() == []


# ============================ Lowering fragments ===============================
def test_initiators_lower_to_channel_trigger():
    for cls in NEW_INITIATORS:
        frag = cls(config={"agent_id": "wf-1"}).lower()
        assert frag["id"] == "wf-1"
        assert frag["trigger"] == {"type": "channel"}


def test_file_destination_lowers_mode_and_target():
    frag = FileDestination(config={"target": "/out.txt", "mode": "append"}).lower()
    assert frag["delivery"] == {"channel": "file", "target": "/out.txt", "mode": "append"}


def test_web_destination_lowers_method_and_target():
    frag = WebDestination(config={"target": "https://api/x", "method": "PUT"}).lower()
    assert frag["delivery"] == {"channel": "web", "target": "https://api/x", "method": "PUT"}


# ============================ Project → GraphRecord ============================
def _init_node(node_type, nid=1, **props):
    props.setdefault("agent_id", "wf")
    return {
        "id": nid, "type": node_type, "properties": props,
        "outputs": [{"name": "out", "links": [1]}],
    }


def test_file_initiator_to_web_destination_lowers_without_advisory_crash():
    """A composition using the NEW kinds lowers to a GraphRecord with NO unsupported-kind
    warning (Phase-05 advisory path) — these are now SUPPORTED."""
    comp = {
        "nodes": [
            _init_node("file_initiator", nid=1, watch_path="/inbox", match="*.pdf"),
            {"id": 2, "type": "agent", "properties": {"persona": "p"},
             "inputs": [{"name": "in", "link": 1}], "outputs": [{"name": "out", "links": [2]}]},
            {"id": 3, "type": "web_destination", "properties": {"target": "https://api/x"},
             "inputs": [{"name": "in", "link": 2}]},
        ],
        "links": [[1, 1, 0, 2, 0, "string"], [2, 2, 0, 3, 0, "string"]],
    }
    rec, warnings = lower_project("u-new", "NewKinds", comp)
    assert isinstance(rec, GraphRecord)
    assert rec.entry == "file_initiator:1"
    kinds = {n.kind for n in rec.nodes}
    assert kinds == {"initiator", "agent", "destination"}
    # none of the new kinds produced an "unsupported"/"unknown block type" advisory.
    joined = " ".join(warnings)
    assert "unsupported block type" not in joined
    assert "unknown block type" not in joined
    # the destination node carries the file/web channel from lower().
    dest = next(n for n in rec.nodes if n.kind == "destination")
    assert dest.config["channel"] == "web"


def test_stt_initiator_lowers_and_binds_asset_ref():
    comp = {
        "nodes": [
            _init_node("stt_initiator", nid=1, agent_id="voice-wf", stream_id="s1"),
            {"id": 2, "type": "agent", "properties": {"persona": "p"},
             "inputs": [{"name": "in", "link": 1}], "outputs": [{"name": "out", "links": [2]}]},
            {"id": 3, "type": "file_destination", "properties": {"target": "/out.txt"},
             "inputs": [{"name": "in", "link": 2}]},
        ],
        "links": [[1, 1, 0, 2, 0, "string"], [2, 2, 0, 3, 0, "string"]],
    }
    rec, warnings = lower_project("u-stt", "Stt", comp)
    init = next(n for n in rec.nodes if n.kind == "initiator")
    assert init.asset_ref == "voice-wf"  # bound via agent_id
    assert init.config == {"type": "channel"}
    dest = next(n for n in rec.nodes if n.kind == "destination")
    assert dest.config["channel"] == "file"


# ============================ Executor delivery (mocked IO) =====================
def _settings():
    return dataclasses.replace(Settings(), runs_stream_id="runs", sender_id="agent-runtime")


class FakeAgentServer:
    def __init__(self, answer):
        self._answer = answer
        self.calls = []

    async def chat(self, model, messages, *, tools=None, overrides=None):
        self.calls.append({"messages": list(messages)})
        return {"role": "assistant", "content": self._answer}


class FakeBus:
    def __init__(self):
        self.published = []

    def stream_key(self, sid):
        return f"stream:{sid}"

    async def publish(self, stream, env):
        self.published.append((stream, env))
        return "1-0"


def _agent_record_config():
    return {
        "version": "0.1", "uid": "00000000-0000-4000-8000-00000000ca11",
        "name": "wf", "brain": {"persona": "p"},
        "input": {"template": "{input}", "vars": {}},
        "delivery": {"channel": "bus", "target": "unused"},
    }


def _graph_with_destination(dest_config):
    return GraphRecord(
        version="0.1", uid="u-dest", name="wf",
        nodes=[
            GraphNode(id="init", kind="initiator", config={"type": "channel"}),
            GraphNode(id="agent", kind="agent", config={"record": _agent_record_config()}),
            GraphNode(id="dest", kind="destination", config=dest_config),
        ],
        edges=[GraphEdge(src="init", dst="agent"), GraphEdge(src="agent", dst="dest")],
    )


def _env(cid="wf"):
    return new_event(stream_id="farm", cid=cid, sid=1, sender="test",
                     event_type="schedule.fired", data={"task": "go"})


async def test_file_destination_delivers_via_mocked_writer():
    writes: list[tuple[str, str, str]] = []

    def fake_writer(path, text, mode):
        writes.append((path, text, mode))
        return path

    runner = Runner(
        _settings(), FakeBus(),
        agent_server=FakeAgentServer("FINAL ANSWER"),
        file_writer=fake_writer,
    )
    g = _graph_with_destination(
        {"channel": "file", "target": "/data/out.txt", "mode": "append"}
    )
    await runner.run_graph_record(g, _env())

    assert writes == [("/data/out.txt", "FINAL ANSWER", "append")]


async def test_web_destination_delivers_via_mocked_caller():
    calls: list[tuple[str, str, str]] = []

    def fake_caller(method, url, text):
        calls.append((method, url, text))
        return 200

    runner = Runner(
        _settings(), FakeBus(),
        agent_server=FakeAgentServer("FINAL ANSWER"),
        web_caller=fake_caller,
    )
    g = _graph_with_destination(
        {"channel": "web", "target": "https://api/x", "method": "PUT"}
    )
    await runner.run_graph_record(g, _env())

    assert calls == [("PUT", "https://api/x", "FINAL ANSWER")]


# ============================ Deliverer units (mocked IO) =======================
async def test_deliver_file_uses_injected_writer():
    seen = []
    out = await deliver_file(
        "/p", "body", mode="overwrite",
        writer=lambda p, t, m: seen.append((p, t, m)) or "id-1",
    )
    assert out == "id-1"
    assert seen == [("/p", "body", "overwrite")]


async def test_deliver_file_rejects_empty_path_and_bad_mode():
    with pytest.raises(DeliveryError):
        await deliver_file("", "x", writer=lambda *a: "x")
    with pytest.raises(DeliveryError):
        await deliver_file("/p", "x", mode="nope", writer=lambda *a: "x")


async def test_deliver_web_uses_injected_caller():
    out = await deliver_web("https://x", "body", method="POST", caller=lambda m, u, t: 201)
    assert out == "201"


async def test_deliver_web_rejects_empty_url():
    with pytest.raises(DeliveryError):
        await deliver_web("", "x", caller=lambda *a: 200)
