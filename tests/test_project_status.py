"""Deploy-status endpoint tests (POST /admin/projects/{uid}/status) — powers Patron's
status badge. Pure FastAPI ``TestClient`` + in-memory fakes; CI-friendly (no containers).

The endpoint is a DRY RUN of Deploy: it lowers the posted composition with the SAME compiler
(``lower_project``) but never persists, then reports compile-ok + deployed + in-sync. These
tests pin the four badge-driving outcomes and the owner-gated non-leak.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.admin import router as admin_router
from agent_runtime.config import settings
from agent_runtime.graph_registry import GraphRegistry

A = "user-A"
B = "user-B"
ADMIN = settings.default_principal


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


# A console_send → agent → bus chain: lowers cleanly, makes no scheduler/ingress binding.
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


def _status(client, uid, user, comp=None, name="P"):
    return client.post(f"/admin/projects/{uid}/status",
                       json={"name": name, "composition": comp or _comp()}, headers=_H(user))


def _deploy(client, uid, user, name="P"):
    return client.post(f"/admin/projects/{uid}/deploy",
                       json={"name": name, "composition": _comp()}, headers=_H(user))


def test_status_not_deployed_but_compiles():
    client, _ = _client()
    r = _status(client, "u1", A).json()
    assert r["ok"] is True
    assert r["deployed"] is False
    assert r["deployed_version"] is None
    assert r["in_sync"] is None       # not deployed → sync is undefined


def test_status_deployed_and_in_sync():
    client, _ = _client()
    assert _deploy(client, "u1", A).json()["ok"] is True
    r = _status(client, "u1", A).json()  # same composition
    assert r["ok"] is True
    assert r["deployed"] is True
    assert r["in_sync"] is True          # exact graph is what's live → green DEPLOYED


def test_status_deployed_but_modified():
    client, _ = _client()
    _deploy(client, "u1", A)
    modified = _comp()
    modified["nodes"] = modified["nodes"][:-1]  # drop the bus destination → structural drift
    modified["links"] = modified["links"][:-1]
    r = _status(client, "u1", A, comp=modified).json()
    assert r["ok"] is True
    assert r["deployed"] is True
    assert r["in_sync"] is False         # deployed, but the graph changed → MODIFIED


def test_status_uncompilable_is_ok_false_not_http_error():
    client, _ = _client()
    r = client.post("/admin/projects/u1/status",
                    json={"name": "P", "composition": {"nodes": [], "links": []}}, headers=_H(A))
    assert r.status_code == 200         # bad-but-well-formed input is data, not an HTTP error
    body = r.json()
    assert body["ok"] is False
    assert body["errors"]               # the reason is surfaced, never swallowed


def test_status_hides_another_owners_deployment():
    client, _ = _client()
    _deploy(client, "u1", A)             # A owns the deployed record
    r = _status(client, "u1", B).json()  # B probes the same uid
    assert r["deployed"] is False        # no existence leak — reported as not-deployed
    assert r["in_sync"] is None


def test_status_admin_sees_any_deployment():
    client, _ = _client()
    _deploy(client, "u1", A)
    r = _status(client, "u1", ADMIN).json()  # admin bypasses ownership
    assert r["deployed"] is True
    assert r["in_sync"] is True
