"""Phase 2: the composer API endpoints.

Builds a bare FastAPI app with just the composer router (no lifespan/bus/registry —
the endpoints are stateless), and drives it with TestClient. Proves the catalog is
served and that /composer/compile lowers the News Agent to the golden DSL over HTTP.
"""

import copy
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.composer_api import router as composer_router

FIXTURES = Path(__file__).parent / "fixtures"
GRAPH = json.loads((FIXTURES / "news_agent.graph.json").read_text())
GOLDEN = json.loads((FIXTURES / "news_agent.golden.json").read_text())

app = FastAPI()
app.include_router(composer_router)
client = TestClient(app)


def test_catalog_endpoint_serves_block_schemas():
    r = client.get("/composer/catalog")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "1.0"
    kinds = {b["type"] for b in body["blocks"]}
    assert {"agent", "trigger", "whatsapp"} <= kinds


def test_compile_endpoint_matches_golden():
    r = client.post("/composer/compile", json=copy.deepcopy(GRAPH))
    assert r.status_code == 200
    assert r.json() == {"ok": GOLDEN["ok"], "dsl": GOLDEN["dsl"], "schedule": GOLDEN["schedule"]}


def test_compile_endpoint_returns_errors_not_500_on_bad_graph():
    # Well-formed JSON, unlowerable graph (no nodes) -> ok:false + errors, HTTP 200.
    r = client.post("/composer/compile", json={"nodes": [], "links": []})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["errors"]


def test_compile_endpoint_handles_non_graph_body():
    r = client.post("/composer/compile", json=["not", "a", "graph"])
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["errors"]
