"""Step 0 verification: the skeleton serves /health (no live stack needed)."""

from fastapi.testclient import TestClient

from agent_runtime.app import app


def test_health_ok():
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["service"] == "agent_runtime"
