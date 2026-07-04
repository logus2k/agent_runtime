"""Step-by-step debug tests (documents/debug_specification.md).

Two layers, both CI-friendly (no containers):
  * the executor pause-gate — a `GraphWorkflowExecutor` with a `DebugSession` pauses before each
    node, advances exactly one per `step()`, aborts on `stop()`, and runs to the end on `cont()`;
  * the control endpoints — `/step` · `/continue` · `/stop` owner-gated, with `session.uid` checks,
    and `/fire {debug:true}` returns a cid to drive them.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.admin import router as admin_router
from agent_runtime.config import settings
from agent_runtime.debug import DebugSession, DebugStopped, registry as debug_registry
from agent_runtime.dsl_graph import GraphEdge, GraphNode, GraphRecord
from agent_runtime.graph_executor import GraphWorkflowExecutor
from agent_runtime.graph_registry import GraphRegistry

A, B = "user-A", "user-B"
ADMIN = settings.default_principal


# --------------------------------------------------------------------------- #
# Executor pause-gate (the core mechanism)
# --------------------------------------------------------------------------- #
def _linear_record():
    # initiator(a) -> agent(b) -> destination(c)
    return GraphRecord(
        uid="t1", name="t1", version="0.1", entry="a",
        nodes=[GraphNode(id="a", kind="initiator"),
               GraphNode(id="b", kind="agent"),
               GraphNode(id="c", kind="destination")],
        edges=[GraphEdge(src="a", dst="b"), GraphEdge(src="b", dst="c")],
    )


def _stepping_executor(session):
    ran: list[str] = []
    paused: "asyncio.Queue[str]" = asyncio.Queue()

    async def h(node, value, ctx):
        ran.append(node.id)
        return value

    async def on_pause(event_type, data):
        await paused.put(data["node"])

    ex = GraphWorkflowExecutor(
        {"initiator": h, "agent": h, "destination": h}, debug=session, on_pause=on_pause
    )
    return ex, ran, paused


async def test_step_advances_one_node_at_a_time():
    session = DebugSession("cid-step", "t1")
    ex, ran, paused = _stepping_executor(session)
    task = asyncio.create_task(ex.run(_linear_record(), None))

    assert await asyncio.wait_for(paused.get(), 1) == "a"   # paused BEFORE the first node
    assert ran == []                                        # nothing ran yet
    session.step()
    assert await asyncio.wait_for(paused.get(), 1) == "b"   # ran a, now paused before b
    assert ran == ["a"]
    session.step()
    assert await asyncio.wait_for(paused.get(), 1) == "c"
    assert ran == ["a", "b"]
    session.cont()                                          # release to finish
    await asyncio.wait_for(task, 1)
    assert ran == ["a", "b", "c"]


async def test_stop_aborts_cleanly():
    session = DebugSession("cid-stop", "t1")
    ex, ran, paused = _stepping_executor(session)
    task = asyncio.create_task(ex.run(_linear_record(), None))
    assert await asyncio.wait_for(paused.get(), 1) == "a"
    session.step()
    await asyncio.wait_for(paused.get(), 1)                 # paused before b
    session.stop()
    try:
        await asyncio.wait_for(task, 1)
        assert False, "expected DebugStopped"
    except DebugStopped:
        pass
    assert ran == ["a"]                                     # b/c never ran


async def test_continue_runs_to_completion_without_pausing():
    session = DebugSession("cid-cont", "t1")
    session.cont()                                          # continue from the very start
    ex, ran, paused = _stepping_executor(session)
    await asyncio.wait_for(ex.run(_linear_record(), None), 1)
    assert ran == ["a", "b", "c"]
    assert paused.empty()                                   # never paused


async def test_non_debug_run_is_unchanged():
    # No DebugSession -> the gate is never awaited; a normal run completes with no pauses.
    ran: list[str] = []

    async def h(node, value, ctx):
        ran.append(node.id)
        return value

    ex = GraphWorkflowExecutor({"initiator": h, "agent": h, "destination": h})  # debug=None
    await asyncio.wait_for(ex.run(_linear_record(), None), 1)
    assert ran == ["a", "b", "c"]


# --------------------------------------------------------------------------- #
# Control endpoints
# --------------------------------------------------------------------------- #
class _FakeBus:
    async def incr(self, *a, **k): return 1
    async def expire(self, *a, **k): return True
    async def publish(self, *a, **k): return "e1"
    def stream_key(self, s): return f"stream:{s}"


class _FakeScheduler:
    async def upsert_schedule(self, *a, **k): return None
    async def upsert_binding(self, *a, **k): return None
    async def count_bindings(self, *a, **k): return 0
    async def delete_schedule(self, *a, **k): return False
    async def delete_binding(self, *a, **k): return False


class _FakeIngress:
    async def bind(self, *a, **k): return {"payload": {}}
    async def unbind_all(self, *a, **k): return 0, []


def _client():
    app = FastAPI()
    app.include_router(admin_router)
    reg = GraphRegistry()
    app.state.graph_registry = reg
    app.state.scheduler_client = _FakeScheduler()
    app.state.ingress_client = _FakeIngress()
    app.state.farm = SimpleNamespace(bus=_FakeBus())
    return TestClient(app), reg


def _H(user):
    return {"X-Patron-User": user}


def _comp():
    return {
        "nodes": [
            {"id": 1, "type": "console_send", "properties": {"message": "hi"},
             "outputs": [{"name": "out", "links": [1]}]},
            {"id": 2, "type": "agent", "properties": {"persona": "p"},
             "inputs": [{"name": "in", "link": 1}], "outputs": [{"name": "out", "links": [2]}]},
            {"id": 3, "type": "bus", "properties": {"target": "s"},
             "inputs": [{"name": "in", "link": 2}]},
        ],
        "links": [[1, 1, 0, 2, 0, "string"], [2, 2, 0, 3, 0, "string"]],
    }


def _deploy(client, uid, user, name="P"):
    return client.post(f"/admin/projects/{uid}/deploy",
                       json={"name": name, "composition": _comp()}, headers=_H(user))


def test_fire_debug_returns_cid_and_flag():
    client, _ = _client()
    _deploy(client, "u1", A)
    r = client.post("/admin/projects/u1/fire", json={"task": "x", "debug": True}, headers=_H(A))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["debug"] is True and body["cid"]


def test_step_continue_stop_owner_gated():
    client, _ = _client()
    _deploy(client, "u1", A)                       # A owns the record
    debug_registry.create("cidX", "u1", A)         # a live session for it
    try:
        # non-owner B → 403
        assert client.post("/admin/projects/u1/step", json={"cid": "cidX"}, headers=_H(B)).status_code == 403
        # owner A → 200 for each verb
        for verb in ("step", "continue", "stop"):
            r = client.post(f"/admin/projects/u1/{verb}", json={"cid": "cidX"}, headers=_H(A))
            assert r.status_code == 200, (verb, r.text)
        # admin bypass
        assert client.post("/admin/projects/u1/step", json={"cid": "cidX"}, headers=_H(ADMIN)).status_code == 200
    finally:
        debug_registry.remove("cidX")


def test_unknown_cid_404_and_wrong_project_409():
    client, _ = _client()
    _deploy(client, "u1", A)
    # unknown cid → 404
    assert client.post("/admin/projects/u1/step", json={"cid": "nope"}, headers=_H(A)).status_code == 404
    # a session that belongs to a DIFFERENT project → 409
    debug_registry.create("cidY", "u2", A)
    try:
        assert client.post("/admin/projects/u1/step", json={"cid": "cidY"}, headers=_H(A)).status_code == 409
    finally:
        debug_registry.remove("cidY")


def test_step_drives_the_session_mode():
    client, _ = _client()
    _deploy(client, "u1", A)
    session = debug_registry.create("cidZ", "u1", A)
    try:
        client.post("/admin/projects/u1/continue", json={"cid": "cidZ"}, headers=_H(A))
        assert session.mode == "continue"
        client.post("/admin/projects/u1/stop", json={"cid": "cidZ"}, headers=_H(A))
        assert session.mode == "stopped"
    finally:
        debug_registry.remove("cidZ")
