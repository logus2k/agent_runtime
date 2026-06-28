"""Admin API: create (assign uid), update by uid, legacy deploy by name, get, list,
delete, validate (dry-run), reload.

Builds a bare FastAPI app with just the admin router (no lifespan/bus), so it tests the
record-management surface offline. The registry instance here is the same object the
router mutates — exactly the sharing the real app relies on for live upserts.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.admin import router as admin_router
from agent_runtime.registry import Registry

NEWS = {
    "version": "0.1",
    "name": "news-morning-ai",
    "trigger": {"type": "schedule"},
    "brain": {"persona": "news_curator", "llm": {"temperature": 0.3, "max_tokens": 1024}},
    "tools": {"server": "mcp", "allow": ["mcp__newsapi_search"], "max_rounds": 3},
    "input": {"template": "Curate {n}.", "vars": {"n": 5}},
    "delivery": {"channel": "whatsapp", "target": "351961050313@c.us"},
}


def _client(tmp_path):
    app = FastAPI()
    app.include_router(admin_router)
    reg = Registry(tmp_path)
    reg.load_all()  # empty dir -> {}
    app.state.registry = reg
    app.state.agents_dir = str(tmp_path)
    return TestClient(app), reg


def test_create_assigns_uid_persists_and_goes_live(tmp_path):
    client, reg = _client(tmp_path)
    r = client.post("/admin/agents", json=NEWS)
    assert r.status_code == 201, r.text
    body = r.json()
    uid = body["uid"]
    assert body["ok"] and body["created"] and body["name"] == "news-morning-ai"
    # file is keyed by uid, not name
    assert (tmp_path / f"{uid}.yaml").exists()
    assert not (tmp_path / "news-morning-ai.yaml").exists()
    # live in the shared registry
    assert reg.get(uid) is not None
    assert reg.get_by_name("news-morning-ai") is not None
    # survives a reload from disk
    rr = client.post("/admin/reload")
    assert rr.status_code == 200 and uid in rr.json()["agents"]


def test_get_and_list(tmp_path):
    client, _ = _client(tmp_path)
    uid = client.post("/admin/agents", json=NEWS).json()["uid"]
    listing = client.get("/admin/agents").json()["agents"]
    assert len(listing) == 1 and listing[0]["name"] == "news-morning-ai"
    assert listing[0]["uid"] == uid
    got = client.get(f"/admin/agents/{uid}").json()
    assert got["brain"]["persona"] == "news_curator" and got["uid"] == uid
    assert client.get("/admin/agents/nope").status_code == 404


def test_update_by_uid_is_in_place(tmp_path):
    client, reg = _client(tmp_path)
    uid = client.post("/admin/agents", json=NEWS).json()["uid"]
    renamed = {**NEWS, "name": "renamed-agent", "uid": uid}
    r = client.put(f"/admin/agents/{uid}", json=renamed)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["replaced"] and body["uid"] == uid and body["name"] == "renamed-agent"
    # same uid, same file — a rename does not spawn a second record
    assert reg.get(uid).name == "renamed-agent"
    assert len(reg.ids) == 1
    assert (tmp_path / f"{uid}.yaml").exists()


def test_legacy_deploy_by_name_reuses_uid(tmp_path):
    """A Patron-style PUT by name (body uses legacy `id`) creates once, then updates the
    same record on re-deploy — no duplicate."""
    client, reg = _client(tmp_path)
    legacy = {**NEWS}
    legacy.pop("name")
    legacy["id"] = "news-morning-ai"
    r1 = client.put("/admin/agents/news-morning-ai", json=legacy)
    assert r1.status_code == 200 and r1.json()["created"]
    uid = r1.json()["uid"]
    r2 = client.put("/admin/agents/news-morning-ai", json=legacy)
    assert r2.status_code == 200 and r2.json()["replaced"]
    assert r2.json()["uid"] == uid
    assert len(reg.ids) == 1


def test_delete_is_hard(tmp_path):
    client, reg = _client(tmp_path)
    uid = client.post("/admin/agents", json=NEWS).json()["uid"]
    assert client.delete(f"/admin/agents/{uid}").status_code == 204
    assert reg.get(uid) is None
    assert not (tmp_path / f"{uid}.yaml").exists()
    assert client.delete(f"/admin/agents/{uid}").status_code == 404


def test_invalid_record_rejected_and_not_written(tmp_path):
    client, _ = _client(tmp_path)
    bad = {**NEWS, "delivery": {"channel": "carrier-pigeon", "target": "x"}}
    r = client.post("/admin/agents", json=bad)
    assert r.status_code == 422
    assert list(tmp_path.glob("*.yaml")) == []  # validation precedes the write


def test_validate_dry_run_never_writes(tmp_path):
    client, _ = _client(tmp_path)
    ok = client.post("/admin/agents/validate", json=NEWS).json()
    assert ok["ok"] and ok["errors"] == []
    bad = client.post("/admin/agents/validate",
                      json={**NEWS, "delivery": {"channel": "nope", "target": "x"}}).json()
    assert not bad["ok"] and bad["errors"]
    assert list(tmp_path.glob("*.yaml")) == []  # dry-run wrote nothing
