"""The composer API endpoints — FIXTURE-DRIVEN where they touch agent data.

Bare FastAPI app with just the composer router (stateless — no lifespan/bus/registry),
driven by TestClient. The catalog is checked against the REGISTERED block types (not a
hardcoded list); /composer/compile is checked against each fixture's golden.
"""

import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.composer import BLOCK_TYPES
from agent_runtime.composer_api import router as composer_router

from _agentfixtures import agent_fixture_params

PAIRS = agent_fixture_params()

app = FastAPI()
app.include_router(composer_router)
client = TestClient(app)


def test_catalog_endpoint_serves_every_registered_block():
    r = client.get("/composer/catalog")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "1.0"
    # the catalog must advertise exactly the registered block types
    assert {b["type"] for b in body["blocks"]} == set(BLOCK_TYPES)


@pytest.mark.parametrize("graph, golden", PAIRS)
def test_compile_endpoint_matches_golden(graph, golden):
    r = client.post("/composer/compile", json=copy.deepcopy(graph))
    assert r.status_code == 200
    assert r.json() == {"ok": golden["ok"], "dsl": golden["dsl"], "schedule": golden["schedule"]}


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
