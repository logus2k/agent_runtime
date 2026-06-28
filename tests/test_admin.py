"""Admin API: deploy (validate + persist + live-upsert), get, list, reload.

Builds a bare FastAPI app with just the admin router (no lifespan/bus), so it tests
the deploy surface offline. The registry instance here is the same object the router
mutates — exactly the sharing the real app relies on for live upserts.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.admin import router as admin_router
from agent_runtime.registry import Registry

NEWS = {
    "version": "0.1",
    "id": "news-morning-ai",
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


def test_deploy_persists_and_goes_live(tmp_path):
    client, reg = _client(tmp_path)
    r = client.put("/admin/agents/news-morning-ai", json=NEWS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["id"] == "news-morning-ai" and body["live"]
    # persisted to <id>.yaml
    assert (tmp_path / "news-morning-ai.yaml").exists()
    # live in the shared registry (the Farm would see it on the next dispatch)
    assert reg.get("news-morning-ai") is not None
    assert "news-morning-ai" in reg.ids
    # reloading from disk keeps it (it was actually written, not just in memory)
    rr = client.post("/admin/reload")
    assert rr.status_code == 200 and "news-morning-ai" in rr.json()["agents"]


def test_get_and_list(tmp_path):
    client, _ = _client(tmp_path)
    client.put("/admin/agents/news-morning-ai", json=NEWS)
    assert client.get("/admin/agents").json()["agents"] == ["news-morning-ai"]
    got = client.get("/admin/agents/news-morning-ai").json()
    assert got["brain"]["persona"] == "news_curator"
    assert client.get("/admin/agents/nope").status_code == 404


def test_invalid_record_rejected_and_not_written(tmp_path):
    client, _ = _client(tmp_path)
    bad = dict(NEWS)
    bad["delivery"] = {"channel": "carrier-pigeon", "target": "x"}  # not a valid channel
    r = client.put("/admin/agents/news-morning-ai", json=bad)
    assert r.status_code == 422
    assert not (tmp_path / "news-morning-ai.yaml").exists()  # validation precedes the write


def test_id_mismatch_rejected(tmp_path):
    client, _ = _client(tmp_path)
    r = client.put("/admin/agents/other-id", json=NEWS)
    assert r.status_code == 400
    assert not (tmp_path / "other-id.yaml").exists()
