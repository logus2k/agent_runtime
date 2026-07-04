"""Multi-tenancy authorization tests (documents/multi_tenancy.md §10).

CI-friendly: pure FastAPI ``TestClient`` with in-memory fakes — no live containers, no
network. Validates that a principal cannot read / deploy / fire / observe / delete another
principal's project through any endpoint, that lists are scoped, that admins bypass, and
that the identity header is only trusted with the shared secret.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime import auth
from agent_runtime.admin import router as admin_router
from agent_runtime.config import settings
from agent_runtime.graph_registry import GraphRegistry

A = "user-A"
B = "user-B"
ADMIN = settings.default_principal  # the default principal is a superuser (ADMIN_PRINCIPALS)


# --------------------------------------------------------------------------- #
# Fakes (nothing hits the network; console_send makes no scheduler/ingress call).
# --------------------------------------------------------------------------- #
class _FakeBus:
    async def incr(self, *a, **k):
        return 1

    async def expire(self, *a, **k):
        return True

    async def publish(self, *a, **k):
        return "entry-1"

    def stream_key(self, s):
        return f"stream:{s}"


class _FakeScheduler:
    async def upsert_schedule(self, *a, **k):
        return None

    async def upsert_binding(self, *a, **k):
        return None

    async def count_bindings(self, *a, **k):
        return 0

    async def delete_schedule(self, *a, **k):
        return False

    async def delete_binding(self, *a, **k):
        return False


class _FakeIngress:
    async def bind(self, *a, **k):
        return {"payload": {}}

    async def unbind_all(self, *a, **k):
        return 0, []


def _client():
    app = FastAPI()
    app.include_router(admin_router)
    reg = GraphRegistry()  # in-memory (no store_dir)
    app.state.graph_registry = reg
    app.state.scheduler_client = _FakeScheduler()
    app.state.ingress_client = _FakeIngress()
    app.state.farm = SimpleNamespace(bus=_FakeBus())
    return TestClient(app), reg


def _H(user):
    return {"X-Patron-User": user}


# A console_send initiator creates NO firing binding (fired manually) — perfect for authz
# tests: deploy touches only the graph registry.
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


# --------------------------------------------------------------------------- #
# Unit: the access primitives.
# --------------------------------------------------------------------------- #
def test_effective_owner_none_is_default():
    assert auth.effective_owner(None) == settings.default_principal
    assert auth.effective_owner(A) == A


def test_can_access_owner_and_admin():
    assert auth.can_access(A, A) is True
    assert auth.can_access(A, B) is False
    assert auth.can_access(A, None) is False          # None -> default; A != default
    assert auth.can_access(ADMIN, A) is True           # admin bypasses ownership
    assert auth.can_access(ADMIN, None) is True


def test_principal_trusts_header_and_falls_back(monkeypatch):
    fake = SimpleNamespace(internal_auth_token="", default_principal="def",
                           admin_principal_set=lambda: {"def"})
    monkeypatch.setattr(auth, "settings", fake)
    assert auth.principal(SimpleNamespace(headers={"X-Patron-User": B})) == B
    assert auth.principal(SimpleNamespace(headers={})) == "def"  # no header -> default


def test_principal_requires_internal_token_when_set(monkeypatch):
    # With a shared secret configured, a spoofed X-Patron-User (no token) is NOT trusted.
    fake = SimpleNamespace(internal_auth_token="secret", default_principal="def",
                           admin_principal_set=lambda: {"def"})
    monkeypatch.setattr(auth, "settings", fake)
    assert auth.principal(SimpleNamespace(headers={"X-Patron-User": B})) == "def"  # spoof ignored
    assert auth.principal(SimpleNamespace(
        headers={"X-Patron-User": B, "X-Internal-Auth": "secret"})) == B           # trusted


# --------------------------------------------------------------------------- #
# Endpoint authorization (the acceptance tests).
# --------------------------------------------------------------------------- #
def test_deploy_stamps_owner_and_get_is_owner_only():
    client, _ = _client()
    assert _deploy(client, "p1", A).status_code == 200
    # owner A can read it; the record carries owner A
    r = client.get("/admin/projects/p1", headers=_H(A))
    assert r.status_code == 200 and r.json()["owner"] == A
    # B is denied
    assert client.get("/admin/projects/p1", headers=_H(B)).status_code == 403
    # admin can read
    assert client.get("/admin/projects/p1", headers=_H(ADMIN)).status_code == 200


def test_redeploy_by_non_owner_is_denied():
    client, _ = _client()
    _deploy(client, "p1", A)
    assert _deploy(client, "p1", B).status_code == 403           # B can't hijack A's uid
    assert _deploy(client, "p1", A).status_code == 200           # A re-deploys fine


def test_list_projects_is_scoped():
    client, _ = _client()
    _deploy(client, "pa", A)
    _deploy(client, "pb", B)
    a_uids = {p["uid"] for p in client.get("/admin/projects", headers=_H(A)).json()["projects"]}
    b_uids = {p["uid"] for p in client.get("/admin/projects", headers=_H(B)).json()["projects"]}
    admin_uids = {p["uid"] for p in client.get("/admin/projects", headers=_H(ADMIN)).json()["projects"]}
    assert a_uids == {"pa"}
    assert b_uids == {"pb"}
    assert {"pa", "pb"} <= admin_uids


def test_fire_is_owner_only():
    client, _ = _client()
    _deploy(client, "p1", A)
    assert client.post("/admin/projects/p1/fire", json={"task": "x"}, headers=_H(B)).status_code == 403
    assert client.post("/admin/projects/p1/fire", json={"task": "x"}, headers=_H(A)).status_code == 200


def test_events_trace_is_owner_only():
    client, _ = _client()
    _deploy(client, "p1", A)
    # non-owner is denied before any stream starts
    assert client.get("/admin/projects/p1/events", headers=_H(B)).status_code == 403


def test_undeploy_and_delete_are_owner_only():
    client, _ = _client()
    _deploy(client, "p1", A)
    assert client.post("/admin/projects/p1/undeploy", json={}, headers=_H(B)).status_code == 403
    assert client.request("DELETE", "/admin/projects/p1", headers=_H(B)).status_code == 403
    assert client.post("/admin/projects/p1/undeploy", json={}, headers=_H(A)).status_code == 200


def test_consistency_is_admin_only():
    client, _ = _client()
    assert client.get("/admin/consistency", headers=_H(A)).status_code == 403
    # admin passes the gate (may 200 or degrade depending on scheduler, but not 403)
    assert client.get("/admin/consistency", headers=_H(ADMIN)).status_code != 403


def test_legacy_record_without_owner_belongs_to_default():
    client, reg = _client()
    _deploy(client, "p1", A)
    # simulate a legacy record: strip its owner
    rec = reg.get("p1")
    reg.upsert(rec.model_copy(update={"owner": None, "owner_email": None}))
    # the default principal (admin) can access; a random user cannot
    assert client.get("/admin/projects/p1", headers=_H(ADMIN)).status_code == 200
    assert client.get("/admin/projects/p1", headers=_H(B)).status_code == 403
