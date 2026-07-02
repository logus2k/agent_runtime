"""Phase 05 — Deploy / Undeploy / Delete a Patron Project (§9.3, §9.3.1, §9.4).

Covers the phase's exit criteria (implementation_plan/05):
  * Deploy a Trigger→Agent→WhatsApp composition -> exactly ONE GraphRecord in the
    registry, keyed by the Project uid, with the firing binding established.
  * Re-deploy after an edit -> the SAME uid updated in place + version bumped (never
    duplicated); the firing binding is upserted (not re-created).
  * Undeploy removes the record AND its firing binding; source assets untouched.
  * Delete a Project = undeploy (runtime side).
  * A composition with NO initiator deploys anyway but returns a "no initiator" warning.
  * Advisory validation never refuses a deploy (unbound blocks warn, still deploy).

The scheduler HTTP is MOCKED via a fake injected on ``app.state.scheduler_client`` — no
live agent_scheduler is needed. The graph registry is the same object the router mutates.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.admin import router as admin_router
from agent_runtime.deploy import SchedulerClient, binding_id_for, schedule_id_for
from agent_runtime.graph_registry import GraphRegistry


# --- fake scheduler (records calls; no HTTP) ----------------------------------
class FakeScheduler:
    """Stands in for ``deploy.SchedulerClient`` — same async method surface. Idempotent
    in-memory schedule/binding stores so a re-deploy PATCHes rather than duplicates."""

    def __init__(self) -> None:
        self.schedules: dict[str, dict[str, Any]] = {}
        self.bindings: dict[str, dict[str, Any]] = {}
        self.upsert_schedule_calls: list[dict[str, Any]] = []
        self.upsert_binding_calls: list[dict[str, Any]] = []
        self.delete_binding_calls: list[tuple[str, str]] = []

    async def upsert_schedule(self, schedule_id: str, *, cron: str, timezone: str = "") -> dict:
        self.upsert_schedule_calls.append(
            {"schedule_id": schedule_id, "cron": cron, "timezone": timezone}
        )
        self.schedules[schedule_id] = {"cron": cron, "timezone": timezone}
        return {"schedule_id": schedule_id}

    async def upsert_binding(
        self, schedule_id: str, binding_id: str, *,
        target_stream_id: str, event_type: str, event_data: dict, room=None,
    ) -> dict:
        self.upsert_binding_calls.append(
            {
                "schedule_id": schedule_id, "binding_id": binding_id,
                "target_stream_id": target_stream_id, "event_type": event_type,
                "event_data": event_data,
            }
        )
        self.bindings[binding_id] = {
            "schedule_id": schedule_id, "target_stream_id": target_stream_id,
            "event_type": event_type, "event_data": event_data,
        }
        return {"binding_id": binding_id}

    async def delete_binding(self, schedule_id: str, binding_id: str) -> bool:
        self.delete_binding_calls.append((schedule_id, binding_id))
        return self.bindings.pop(binding_id, None) is not None


# --- composition builders (litegraph serialize() shape) -----------------------
def _trigger_agent_whatsapp(*, cron="0 7 * * *", timezone="Europe/Lisbon",
                            persona="news_curator", target="120363427427912302@g.us") -> dict:
    """A minimal Trigger -> Agent -> WhatsApp composition, wired with two real links."""
    return {
        "version": 0.4,
        "nodes": [
            {
                "id": 1, "type": "trigger",
                "properties": {
                    "agent_id": "ai-morning-news", "trigger_type": "schedule",
                    "cron": cron, "timezone": timezone,
                },
                "outputs": [{"name": "out", "links": [1]}],
            },
            {
                "id": 2, "type": "agent",
                "properties": {
                    "persona": persona, "temperature": 0.3, "max_tokens": 1024,
                    "input_template": "Curate {n}.", "input_vars": {"n": 5},
                },
                "inputs": [{"name": "in", "link": 1}],
                "outputs": [{"name": "out", "links": [2]}],
            },
            {
                "id": 3, "type": "whatsapp",
                "properties": {"target": target, "target_name": "L2K Chat"},
                "inputs": [{"name": "in", "link": 2}],
            },
        ],
        "links": [
            [1, 1, 0, 2, 0, "string"],
            [2, 2, 0, 3, 0, "string"],
        ],
    }


def _no_initiator() -> dict:
    """Agent -> WhatsApp with NO Trigger: it can never fire (a warning, not a refusal)."""
    return {
        "version": 0.4,
        "nodes": [
            {
                "id": 2, "type": "agent",
                "properties": {"persona": "news_curator", "input_template": "hi"},
                "outputs": [{"name": "out", "links": [2]}],
            },
            {
                "id": 3, "type": "whatsapp",
                "properties": {"target": "x@g.us"},
                "inputs": [{"name": "in", "link": 2}],
            },
        ],
        "links": [[2, 2, 0, 3, 0, "string"]],
    }


def _with_transform() -> dict:
    """Trigger -> Agent -> Transform -> WhatsApp. The Transform block is inert in the
    runtime record (it lowers to no GraphRecord node); it must be SKIPPED with a warning,
    never crash the deploy with a pydantic ValidationError -> 500 (DEFECT 1, §9.3)."""
    return {
        "version": 0.4,
        "nodes": [
            {
                "id": 1, "type": "trigger",
                "properties": {"agent_id": "ai-morning-news", "trigger_type": "schedule",
                               "cron": "0 7 * * *"},
                "outputs": [{"name": "out", "links": [1]}],
            },
            {
                "id": 2, "type": "agent",
                "properties": {"persona": "news_curator"},
                "inputs": [{"name": "in", "link": 1}],
                "outputs": [{"name": "out", "links": [2]}],
            },
            {
                "id": 5, "type": "transform",
                "properties": {},
                "inputs": [{"name": "in", "link": 2}],
                "outputs": [{"name": "out", "links": [3]}],
            },
            {
                "id": 3, "type": "whatsapp",
                "properties": {"target": "x@g.us"},
                "inputs": [{"name": "in", "link": 3}],
            },
        ],
        "links": [
            [1, 1, 0, 2, 0, "string"],
            [2, 2, 0, 5, 0, "string"],
            [3, 5, 0, 3, 0, "string"],
        ],
    }


def _multi_root_no_initiator() -> dict:
    """Two disconnected Agent -> Destination chains and NO Trigger: no single entry root
    can be derived. It must deploy with a warning, never crash with a GraphRecord
    'cannot derive a single entry node' ValidationError -> 500 (DEFECT 2, §9.3)."""
    return {
        "version": 0.4,
        "nodes": [
            {
                "id": 2, "type": "agent", "properties": {"persona": "p"},
                "outputs": [{"name": "out", "links": [1]}],
            },
            {
                "id": 3, "type": "whatsapp", "properties": {"target": "a@g.us"},
                "inputs": [{"name": "in", "link": 1}],
            },
            {
                "id": 4, "type": "agent", "properties": {"persona": "q"},
                "outputs": [{"name": "out", "links": [2]}],
            },
            {
                "id": 5, "type": "bus", "properties": {"target": "b"},
                "inputs": [{"name": "in", "link": 2}],
            },
        ],
        "links": [[1, 2, 0, 3, 0, "string"], [2, 4, 0, 5, 0, "string"]],
    }


# --- test app -----------------------------------------------------------------
def _client():
    app = FastAPI()
    app.include_router(admin_router)
    reg = GraphRegistry()
    sched = FakeScheduler()
    app.state.graph_registry = reg
    app.state.scheduler_client = sched
    return TestClient(app), reg, sched


PUID = "121c7e15-5f7f-4969-8363-02d6315dd777"


# ============================ Deploy ==========================================
def test_deploy_creates_one_graph_record_and_firing_binding():
    client, reg, sched = _client()
    r = client.post(
        f"/admin/projects/{PUID}/deploy",
        json={"name": "News Project", "composition": _trigger_agent_whatsapp()},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["uid"] == PUID
    assert body["version"] == "0.1"
    assert body["warnings"] == []  # a fully-wired, bound composition warns nothing

    # exactly ONE record in the registry, keyed by the project uid.
    assert reg.uids == [PUID]
    rec = reg.require(PUID)
    kinds = sorted(n.kind for n in rec.nodes)
    assert kinds == ["agent", "destination", "initiator"]
    # 2 typed edges (trigger->agent, agent->whatsapp).
    assert len(rec.edges) == 2

    # the firing binding was established: schedule + binding whose event_data ties back
    # to THIS record (record_uid), targeting the farm stream.
    assert body["firing"]["bound"] is True
    assert len(sched.upsert_schedule_calls) == 1
    assert sched.upsert_schedule_calls[0]["cron"] == "0 7 * * *"
    assert len(sched.upsert_binding_calls) == 1
    bind = sched.upsert_binding_calls[0]
    assert bind["event_data"]["record_uid"] == PUID
    assert bind["target_stream_id"]  # the farm ingress stream


def test_redeploy_after_edit_updates_same_uid_and_bumps_version():
    client, reg, sched = _client()
    client.post(
        f"/admin/projects/{PUID}/deploy",
        json={"name": "News Project", "composition": _trigger_agent_whatsapp()},
    )
    assert reg.require(PUID).version == "0.1"

    # edit the composition (new persona + new target) and re-deploy the SAME uid.
    edited = _trigger_agent_whatsapp(persona="news_curator_v2", target="999@g.us")
    r2 = client.post(
        f"/admin/projects/{PUID}/deploy",
        json={"name": "News Project (edited)", "composition": edited},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["version"] == "0.2"  # minor bumped

    # NO duplicate — still exactly one record, updated in place.
    assert reg.uids == [PUID]
    rec = reg.require(PUID)
    assert rec.name == "News Project (edited)"
    agent = next(n for n in rec.nodes if n.kind == "agent")
    assert agent.asset_ref == "news_curator_v2"
    dest = next(n for n in rec.nodes if n.kind == "destination")
    assert dest.asset_ref == "999@g.us"

    # the firing binding was upserted again (idempotent), same ids — not duplicated.
    assert len(sched.upsert_binding_calls) == 2
    ids = {c["binding_id"] for c in sched.upsert_binding_calls}
    assert len(ids) == 1  # same binding id both times


# ============================ Undeploy / Delete ===============================
def test_undeploy_removes_record_and_firing_binding():
    client, reg, sched = _client()
    client.post(
        f"/admin/projects/{PUID}/deploy",
        json={"name": "News Project", "composition": _trigger_agent_whatsapp()},
    )
    assert reg.get(PUID) is not None
    assert sched.bindings  # a binding exists

    r = client.post(f"/admin/projects/{PUID}/undeploy")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["removed"] is True and body["firing_removed"] is True

    # record gone AND its firing binding removed.
    assert reg.get(PUID) is None
    assert reg.uids == []
    assert sched.bindings == {}
    assert sched.delete_binding_calls  # the scheduler was asked to remove it


def test_undeploy_unknown_uid_is_reported_not_error():
    client, reg, _ = _client()
    r = client.post(f"/admin/projects/{PUID}/undeploy")
    assert r.status_code == 200
    body = r.json()
    assert body["removed"] is False
    assert any("no live graph record" in w for w in body["warnings"])


def test_delete_project_undeploys_runtime_side():
    client, reg, sched = _client()
    client.post(
        f"/admin/projects/{PUID}/deploy",
        json={"name": "News Project", "composition": _trigger_agent_whatsapp()},
    )
    r = client.delete(f"/admin/projects/{PUID}")
    assert r.status_code == 200, r.text
    assert r.json()["removed"] is True
    assert reg.get(PUID) is None
    assert sched.bindings == {}


# ============================ Advisory validation =============================
def test_no_initiator_deploys_but_warns():
    client, reg, sched = _client()
    r = client.post(
        f"/admin/projects/{PUID}/deploy",
        json={"name": "Headless Project", "composition": _no_initiator()},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # it STILL deploys (a record exists) — advisory validation never refuses.
    assert body["ok"] is True
    assert reg.get(PUID) is not None
    # but it warns about the missing initiator, and no firing binding was created.
    assert any("no initiator" in w for w in body["warnings"])
    assert body["firing"]["bound"] is False
    assert sched.upsert_binding_calls == []


def test_unbound_agent_warns_but_still_deploys():
    client, reg, _ = _client()
    comp = _trigger_agent_whatsapp()
    # strip the agent's persona binding -> unbound + missing required config.
    for n in comp["nodes"]:
        if n["type"] == "agent":
            n["properties"].pop("persona", None)
    r = client.post(
        f"/admin/projects/{PUID}/deploy",
        json={"name": "Unbound Project", "composition": comp},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and reg.get(PUID) is not None  # deployed anyway
    assert any("unbound" in w.lower() or "persona" in w.lower() for w in body["warnings"])


def test_empty_composition_is_the_one_hard_failure():
    client, _, _ = _client()
    r = client.post(
        f"/admin/projects/{PUID}/deploy",
        json={"name": "Empty", "composition": {"nodes": [], "links": []}},
    )
    # nothing to deploy at all -> 422 (not advisory; there is no record to create).
    assert r.status_code == 422, r.text


def test_transform_block_deploys_with_warning_not_500():
    """DEFECT 1: a composition containing a Transform block must NOT crash deploy with an
    uncaught pydantic ValidationError -> 500. Per §9.3 it becomes a WARNING; the Transform
    is skipped and the rest of the record still deploys (200)."""
    client, reg, _ = _client()
    r = client.post(
        f"/admin/projects/{PUID}/deploy",
        json={"name": "With Transform", "composition": _with_transform()},
    )
    assert r.status_code == 200, r.text  # NOT 500
    body = r.json()
    assert body["ok"] is True
    # the record deployed, minus the inert transform node.
    rec = reg.require(PUID)
    kinds = sorted(n.kind for n in rec.nodes)
    assert "initiator" in kinds and "agent" in kinds
    assert all(k != "transform" for k in kinds)  # transform is not a runtime node
    # and it was surfaced as an advisory warning.
    assert any("transform" in w.lower() for w in body["warnings"]), body["warnings"]


def test_multi_root_no_initiator_deploys_with_warning_not_500():
    """DEFECT 2: a no-initiator composition with more than one root node must NOT crash
    deploy with a GraphRecord 'cannot derive a single entry' ValidationError -> 500. Per
    §9.3 it becomes a WARNING and deploys anyway (200), with a fallback entry pinned."""
    client, reg, _ = _client()
    r = client.post(
        f"/admin/projects/{PUID}/deploy",
        json={"name": "Multi Root", "composition": _multi_root_no_initiator()},
    )
    assert r.status_code == 200, r.text  # NOT 500
    body = r.json()
    assert body["ok"] is True
    rec = reg.require(PUID)
    assert rec.entry in {n.id for n in rec.nodes}  # a valid entry was pinned
    joined = " ".join(body["warnings"]).lower()
    assert "no initiator" in joined
    assert "root" in joined  # the multiple-roots condition is flagged


# ============================ List / get ======================================
def test_list_and_get_deployed_projects():
    client, _, _ = _client()
    client.post(
        f"/admin/projects/{PUID}/deploy",
        json={"name": "News Project", "composition": _trigger_agent_whatsapp()},
    )
    listing = client.get("/admin/projects").json()["projects"]
    assert len(listing) == 1 and listing[0]["uid"] == PUID
    assert listing[0]["nodes"] == 3 and listing[0]["edges"] == 2

    got = client.get(f"/admin/projects/{PUID}").json()
    assert got["uid"] == PUID and got["name"] == "News Project"
    assert client.get("/admin/projects/nope").status_code == 404


# ============================ SchedulerClient real HTTP (mocked transport) =====
async def test_scheduler_client_upsert_is_idempotent_over_http():
    """The REAL SchedulerClient HTTP path: a first create (201), then a re-deploy that
    409s on the existing schedule/binding and falls back to PATCH — never a duplicate."""
    seen: list[tuple[str, str]] = []
    existing: set[str] = set()

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        path = req.url.path
        if req.method == "POST":
            key = path  # /schedules or /schedules/{id}/bindings
            if key in existing:
                return httpx.Response(409, json={"detail": "already exists"})
            existing.add(key)
            return httpx.Response(201, json={"ok": True})
        if req.method == "PATCH":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    client = SchedulerClient("http://sched.test", transport=httpx.MockTransport(handler))
    sid = schedule_id_for(PUID)
    bid = binding_id_for(PUID)

    # first deploy: both create (201).
    await client.upsert_schedule(sid, cron="0 7 * * *", timezone="Europe/Lisbon")
    await client.upsert_binding(
        sid, bid, target_stream_id="agent-runtime",
        event_type="schedule.fired", event_data={"record_uid": PUID},
    )
    # re-deploy: POST 409 -> PATCH fallback for BOTH.
    await client.upsert_schedule(sid, cron="0 8 * * *", timezone="Europe/Lisbon")
    await client.upsert_binding(
        sid, bid, target_stream_id="agent-runtime",
        event_type="schedule.fired", event_data={"record_uid": PUID},
    )
    methods = [m for m, _ in seen]
    assert methods.count("POST") == 4   # two schedules + two bindings attempted
    assert methods.count("PATCH") == 2  # both re-deploys fell back to PATCH


async def test_scheduler_client_delete_binding_handles_404():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404 if "missing" in req.url.path else 204)

    client = SchedulerClient("http://sched.test", transport=httpx.MockTransport(handler))
    assert await client.delete_binding("sched-x", "bind-present") is True
    assert await client.delete_binding("sched-missing", "bind-missing") is False
