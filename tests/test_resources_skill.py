"""The 'skill' resource source (§8.3): GET /resources/skill lists name+description like
/resources/mcp-tool, and the descriptor is declared multi (a checklist picker)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import agent_runtime.skills.registry as skills_registry
from agent_runtime.resource import descriptor_by_id
from agent_runtime.resources_api import router as resources_router
from agent_runtime.skills.registry import SkillRegistry

FIXTURES = Path(__file__).parent / "skill_fixtures"

app = FastAPI()
app.include_router(resources_router)
client = TestClient(app)


def test_skill_descriptor_declared_multi():
    d = descriptor_by_id("skill")
    assert d is not None
    assert d.source == "skill"
    assert d.multi is True  # a multi-select checklist, like mcp-tool
    assert d.identity == "name"


def test_skill_in_catalog():
    r = client.get("/resources/catalog")
    assert r.status_code == 200
    ids = {res["id"] for res in r.json()["resources"]}
    assert "skill" in ids


def test_list_skill_resource(monkeypatch):
    # Bind the process singleton to the test fixtures so the source lists them.
    monkeypatch.setattr(
        skills_registry, "_registry", SkillRegistry(str(FIXTURES)), raising=False
    )
    r = client.get("/resources/skill")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    names = {i["name"] for i in body["items"]}
    assert {"news-curation", "deep-analysis", "market-rules"} <= names
    # each item carries a description (the picker column)
    by_name = {i["name"]: i for i in body["items"]}
    assert by_name["news-curation"]["description"].startswith("How to curate")
