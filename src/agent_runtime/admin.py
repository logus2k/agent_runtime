"""Admin API — manage agent records at runtime, no restart.

Backs the administration frontend (documents/administration_frontend.md) and the
Patron → agent_runtime deploy bridge. Records are validated, persisted to
``data/agents/<uid>.yaml`` (the file is keyed by the immutable uid, so a rename never
moves it), and upserted into the LIVE registry the Farm reads — so an agent runs with
the new settings on its next trigger, no process restart.

Identity model (§4.0): identity is a server-assigned immutable ``uid`` (UUIDv4); ``name``
is an editable label. Routing keys on the uid; the name is denormalised for display.

  GET    /admin/agents             : rich list (uid, name, summary)
  GET    /admin/agents/{uid}        : fetch one full record
  POST   /admin/agents             : create — server assigns + returns the uid
  PUT    /admin/agents/{key}        : upsert by uid (UI) or by name (legacy Patron deploy)
  DELETE /admin/agents/{uid}        : hard-delete (file + live registry)
  POST   /admin/agents/validate     : dry-run validate a candidate record (never writes)
  GET    /admin/runs                : recent run events from the runs stream
  POST   /admin/reload              : reload every record from disk

The surface is localhost-bound (compose publishes 127.0.0.1:6817) and fronted +
auth-gated by nginx for any external access. If it is ever widened, add a shared
secret check here (e.g. an ADMIN_TOKEN bearer).
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, ValidationError

from .config import settings
from .dsl import AgentRecord
from .registry import Registry

log = logging.getLogger("agent_runtime.admin")
router = APIRouter(prefix="/admin", tags=["admin"])


# --- helpers -----------------------------------------------------------------

def _registry(request: Request) -> Registry:
    reg = getattr(request.app.state, "registry", None)
    if reg is None:
        raise HTTPException(status_code=503, detail="registry not ready")
    return reg


def _bus(request: Request):
    farm = getattr(request.app.state, "farm", None)
    if farm is None or getattr(farm, "bus", None) is None:
        raise HTTPException(status_code=503, detail="bus not ready")
    return farm.bus


def _agents_dir(request: Request) -> Path:
    return Path(getattr(request.app.state, "agents_dir", settings.agents_dir))


def _write_record_yaml(agents_dir: Path, uid: str, data: dict[str, Any]) -> Path:
    """Atomically write ``<uid>.yaml`` — keyed by uid so a rename never moves it."""
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / f"{uid}.yaml"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    tmp.replace(path)  # atomic
    return path


def _normalize_body(body: dict) -> dict:
    """Tolerate the legacy DSL shape (``id`` instead of ``uid``/``name``).

    Patron's compiler emits ``id``; map it to ``name`` so deploys keep working until
    Patron carries the uid itself (see documents/administration_frontend.md §4.0)."""
    body = dict(body)
    if "id" in body:
        body.setdefault("name", body["id"])
        body.pop("id", None)
    return body


def _validate_record(body: dict, *, uid: str) -> AgentRecord:
    """Validate a candidate record with a concrete uid, raising HTTP 422 on failure."""
    data = {**_normalize_body(body), "uid": uid}
    try:
        return AgentRecord.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"invalid agent record: {exc}")


def _summary(rec: AgentRecord) -> dict:
    """Compact projection for the list view (avoids N full-record fetches)."""
    return {
        "uid": rec.uid,
        "name": rec.name,
        "description": rec.description,
        "enabled": rec.enabled,
        "trigger_type": rec.trigger.type,
        "persona": rec.brain.persona,
        "tools_server": rec.tools.server if rec.tools else None,
        "tools_count": len(rec.tools.allow) if rec.tools else 0,
        "delivery_channel": rec.delivery.channel,
        "delivery_target": rec.delivery.target,
    }


def _persist_and_upsert(request: Request, record: AgentRecord) -> Path:
    data = record.model_dump(mode="json", exclude_none=True)
    path = _write_record_yaml(_agents_dir(request), record.uid, data)
    _registry(request).upsert(record)
    return path


# --- read --------------------------------------------------------------------

@router.get("/agents")
async def list_agents(request: Request, detail: int = 0) -> dict:
    reg = _registry(request)
    if detail:
        return {"agents": [r.model_dump(mode="json", exclude_none=True) for r in reg.all()]}
    return {"agents": [_summary(r) for r in reg.all()]}


@router.get("/agents/{uid}")
async def get_agent(uid: str, request: Request) -> dict:
    rec = _registry(request).get(uid)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"no agent '{uid}'")
    return rec.model_dump(mode="json", exclude_none=True)


# --- write -------------------------------------------------------------------

@router.post("/agents", status_code=201)
async def create_agent(body: dict, request: Request) -> dict:
    """Create a new record. The server assigns a fresh immutable uid and returns it."""
    uid = str(uuid.uuid4())
    record = _validate_record(body, uid=uid)
    path = _persist_and_upsert(request, record)
    log.info("created agent '%s' (%s) -> %s", record.name, uid, path)
    return {"ok": True, "uid": uid, "name": record.name, "path": str(path), "created": True}


@router.put("/agents/{key}")
async def deploy_agent(key: str, body: dict, request: Request) -> dict:
    """Upsert. ``key`` is a uid (admin UI edit) or, for legacy Patron deploys, a name.

    Resolution order for the target record's uid: the path if it is a known uid; else an
    existing record with the same name (so a re-deploy updates in place, no duplicate);
    else a fresh uid. The name change is just a field update — the file stays <uid>.yaml."""
    reg = _registry(request)
    norm = _normalize_body(body)
    name = norm.get("name") or key

    existing = reg.get(key) or reg.get_by_name(key) or (
        reg.get_by_name(name) if name else None
    )
    if existing is not None and norm.get("uid") and norm["uid"] != existing.uid:
        raise HTTPException(
            status_code=400,
            detail=f"body uid '{norm['uid']}' != target record uid '{existing.uid}'",
        )
    uid = existing.uid if existing else (norm.get("uid") or str(uuid.uuid4()))
    norm.setdefault("name", name)

    record = _validate_record(norm, uid=uid)
    path = _persist_and_upsert(request, record)
    replaced = existing is not None
    log.info(
        "%s agent '%s' (%s) -> %s",
        "replaced" if replaced else "created", record.name, uid, path,
    )
    return {
        "ok": True, "uid": uid, "name": record.name, "path": str(path),
        "created": not replaced, "replaced": replaced,
    }


@router.delete("/agents/{uid}", status_code=204)
async def delete_agent(uid: str, request: Request) -> Response:
    """Hard-delete: remove the record file and drop it from the live registry."""
    reg = _registry(request)
    if reg.get(uid) is None:
        raise HTTPException(status_code=404, detail=f"no agent '{uid}'")
    path = _agents_dir(request) / f"{uid}.yaml"
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not delete file: {exc}")
    reg.delete(uid)
    log.info("deleted agent %s (%s)", uid, path)
    return Response(status_code=204)


async def _set_enabled(request: Request, uid: str, value: bool) -> dict:
    reg = _registry(request)
    rec = reg.get(uid)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"no agent '{uid}'")
    updated = rec.model_copy(update={"enabled": value})
    path = _persist_and_upsert(request, updated)
    log.info("agent '%s' (%s) enabled=%s -> %s", updated.name, uid, value, path)
    return {"ok": True, "uid": uid, "name": updated.name, "enabled": value}


@router.post("/agents/{uid}/enable")
async def enable_agent(uid: str, request: Request) -> dict:
    """Mark the agent active — the farm will run it on trigger again."""
    return await _set_enabled(request, uid, True)


@router.post("/agents/{uid}/disable")
async def disable_agent(uid: str, request: Request) -> dict:
    """Mark the agent inactive — the farm skips it on trigger (record is kept)."""
    return await _set_enabled(request, uid, False)


@router.post("/agents/validate")
async def validate_agent(body: dict, request: Request) -> dict:
    """Dry-run: validate a candidate record without writing anything.

    Returns ``{ok, errors}``. A real uid is only needed to pass the schema; we use the
    body's uid if present, else a throwaway, so the rest of the record is checked."""
    probe_uid = _normalize_body(body).get("uid") or str(uuid.uuid4())
    data = {**_normalize_body(body), "uid": probe_uid}
    try:
        AgentRecord.model_validate(data)
    except ValidationError as exc:
        return {"ok": False, "errors": [
            {"loc": ".".join(str(p) for p in e["loc"]), "msg": e["msg"]}
            for e in exc.errors()
        ]}
    return {"ok": True, "errors": []}


# --- observability -----------------------------------------------------------

@router.get("/runs")
async def list_runs(
    request: Request,
    agent_uid: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict:
    """Recent run events from the runs stream (newest first). Optionally filtered to one
    agent_uid. Read-only XREAD replay — bounded scan, fine at this volume."""
    bus = _bus(request)
    stream = bus.stream_key(settings.runs_stream_id)
    # Read a generous window forward, then keep the newest `limit` (after filtering).
    _, envelopes = await bus.observe(stream, "0", count=max(limit * 5, 200))
    events: list[dict] = []
    for env in envelopes:
        d = env.payload.data or {}
        if agent_uid and d.get("agent_uid") != agent_uid:
            continue
        events.append({
            "cid": env.header.cid,
            "sid": env.header.sid,
            "event_type": env.header.event_type,
            "timestamp": env.header.timestamp,
            "agent_uid": d.get("agent_uid"),
            "agent_name": d.get("agent_name"),
            "data": d,
        })
    events.reverse()  # newest first
    return {"runs": events[:limit]}


# --- consistency (job ↔ agent seam) ------------------------------------------

@router.get("/consistency")
async def consistency(request: Request) -> dict:
    """Cross-reference scheduler jobs with agent records to surface creation mistakes:
    **dangling** jobs (point at an agent that doesn't exist → the farm silently drops
    them) and **orphan** agents (no job triggers them → they never run).

    Joined server-side (agent_runtime → scheduler over logus2k_network) to avoid CORS;
    read-only toward the scheduler. If the scheduler is unreachable, jobs come back empty
    and only the agent list is returned (degraded, flagged)."""
    reg = _registry(request)
    agents = reg.all()

    jobs: list[dict] = []
    scheduler_ok = True
    scheduler_error = None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.scheduler_url.rstrip('/')}/jobs")
            resp.raise_for_status()
            jobs = resp.json()
    except Exception as exc:  # noqa: BLE001 - degrade loudly but don't 500 the UI
        scheduler_ok = False
        scheduler_error = str(exc)
        log.warning("consistency: scheduler unreachable at %s: %s", settings.scheduler_url, exc)

    def _resolve(ed: dict) -> Optional[AgentRecord]:
        uid = ed.get("agent_uid")
        if uid:
            return reg.get(uid)
        name = ed.get("agent_name") or ed.get("agent")
        return reg.get_by_name(name) if name else None

    # Per-job linkage + dangling detection.
    by_agent_uid: dict[str, list[dict]] = {}
    dangling: list[dict] = []
    for job in jobs:
        ed = job.get("event_data") or {}
        rec = _resolve(ed)
        job_ref = {
            "job_id": job.get("job_id"),
            "trigger": job.get("trigger"),
            "next_run_time": job.get("next_run_time"),
            "paused": job.get("paused"),
            "agent_uid": ed.get("agent_uid"),
            "agent_name": ed.get("agent_name") or ed.get("agent"),
        }
        if rec is None:
            dangling.append(job_ref)
        else:
            by_agent_uid.setdefault(rec.uid, []).append(job_ref)

    agent_rows = [
        {
            "uid": a.uid,
            "name": a.name,
            "jobs": by_agent_uid.get(a.uid, []),
            "orphan": not by_agent_uid.get(a.uid),
        }
        for a in agents
    ]
    return {
        "scheduler_ok": scheduler_ok,
        "scheduler_error": scheduler_error,
        "agents": agent_rows,
        "dangling": dangling,
        "orphan_count": sum(1 for a in agent_rows if a["orphan"]),
        "dangling_count": len(dangling),
    }


# --- channel target discovery (for the delivery dropdown) --------------------

async def _fetch_whatsapp_targets() -> dict:
    """Connect to the WhatsApp bridge's /agent namespace (same creds delivery uses) and
    list groups + contacts as {id, name, kind}. Read-only; degrades gracefully."""
    if not settings.whatsapp_token:
        return {"targets": [], "bridge_ok": False,
                "error": "WHATSAPP_TOKEN not set — cannot list chats"}

    import socketio  # local import: package loads without socketio at rest

    sio = socketio.AsyncClient()
    targets: list[dict] = []
    try:
        await sio.connect(
            settings.whatsapp_bridge_url, namespaces=["/agent"], wait_timeout=10,
            auth={"agentName": settings.whatsapp_agent_name, "token": settings.whatsapp_token},
        )
        groups = await sio.call("listGroups", {}, namespace="/agent", timeout=15)
        for g in (groups or {}).get("groups", []):
            if g.get("id"):
                targets.append({"id": g["id"], "name": g.get("name") or g["id"], "kind": "group"})
        try:
            contacts = await sio.call("listContacts", {}, namespace="/agent", timeout=15)
            # WhatsApp's LID migration makes the bridge return ~2 rows per person
            # (one keyed on the LID, one on the phone number) sharing the same chat id.
            # Collapse by id and pick the best label: a real (non-numeric) saved name if
            # present, else the phone number that matches the id (never the LID).
            best: dict[str, tuple[int, str]] = {}
            for c in (contacts or {}).get("contacts", []):
                cid = c.get("id")
                if not cid:
                    continue
                local = cid.split("@", 1)[0]
                name = (c.get("name") or "").strip()
                number = (c.get("number") or "").strip()
                real_name = bool(name) and not name.isdigit()
                label = name if real_name else local
                score = (2 if real_name else 0) + (1 if number == local else 0)
                if cid not in best or score > best[cid][0]:
                    best[cid] = (score, label)
            for cid, (_, label) in best.items():
                targets.append({"id": cid, "name": label, "kind": "contact"})
        except Exception as exc:  # noqa: BLE001 - groups still useful if contacts fail
            log.warning("listContacts failed (groups returned): %s", exc)
    except Exception as exc:  # noqa: BLE001 - degrade loudly but don't 500 the UI
        log.warning("whatsapp targets fetch failed: %s", exc)
        return {"targets": [], "bridge_ok": False, "error": str(exc)}
    finally:
        try:
            await sio.disconnect()
        except Exception:  # noqa: BLE001
            pass

    return {"targets": targets, "bridge_ok": True, "error": None}


@router.get("/channels/whatsapp/targets")
async def whatsapp_targets(request: Request) -> dict:
    """Friendly chat list for the delivery-target dropdown: groups + contacts with their
    ids. The UI shows the name and stores the id in delivery.target."""
    return await _fetch_whatsapp_targets()


async def _fetch_mcp_tools() -> dict:
    """List the tools the decoupled mcp-service advertises, for the Agent allow-list
    picker. Names are returned **prefixed** (``mcp__web_search``) — the same namespaced
    vocabulary the DSL allow-list and the LLM specs use, so the picker writes back exactly
    what lowering expects. Read-only; degrades loudly-but-gracefully (never 500s the UI)."""
    from .mcp_client import MCPClient  # local import: keeps the module import-light

    key = settings.mcp_server_key
    client = MCPClient(settings.mcp_url, server=key)
    try:
        raw = await client.list_tools()
    except Exception as exc:  # noqa: BLE001 - surface the failure in the UI, don't 500
        log.warning("mcp tools fetch failed (%s): %s", settings.mcp_url, exc)
        return {"tools": [], "server_ok": False, "error": str(exc)}

    tools: list[dict] = []
    seen: set[str] = set()
    for t in raw:
        name = t.get("name")
        if not name or name in seen:  # the catalog can advertise a tool twice; keep the first
            continue
        seen.add(name)
        tools.append({
            "name": f"{key}__{name}",              # namespaced (matches the allow-list)
            "raw": name,
            "description": t.get("description", ""),
        })
    return {"tools": tools, "server_ok": True, "error": None}


@router.get("/channels/mcp/tools")
async def mcp_tools(request: Request) -> dict:
    """Available MCP tools for the Agent tools allow-list picker: prefixed name +
    description. The UI shows checkboxes and stores the selected names in tools.allow."""
    return await _fetch_mcp_tools()


async def _fetch_presets() -> dict:
    """List the agent_server presets (the ``persona`` a brain references — it holds the
    system prompt + sampling params + memory policy). For the persona dropdown. Read-only;
    degrades loudly-but-gracefully (never 500s the UI)."""
    url = f"{settings.agent_server_url.rstrip('/')}/admin/api/agents"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - surface the failure in the UI, don't 500
        log.warning("presets fetch failed (%s): %s", url, exc)
        return {"presets": [], "server_ok": False, "error": str(exc)}

    presets: list[dict] = []
    for a in (data or {}).get("agents", []):
        name = a.get("name")
        if not name:
            continue
        presets.append({"name": name, "memory_policy": a.get("memory_policy")})
    presets.sort(key=lambda p: p["name"].lower())
    return {"presets": presets, "server_ok": True, "error": None}


@router.get("/channels/presets")
async def presets(request: Request) -> dict:
    """agent_server presets for the persona picker: name (+ memory policy). The UI shows a
    dropdown and stores the chosen name in brain.persona."""
    return await _fetch_presets()


# --- template co-author (the Template Studio's "Improve" loop) ----------------

# The agent_server preset that rewrites/improves input_templates (created out-of-band).
TEMPLATE_WRITER_PRESET = "template_writer"
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class TemplateWriterReq(BaseModel):
    template: str = ""
    instruction: str
    vars: list[str] = Field(default_factory=list)


@router.post("/tools/template-writer")
async def template_writer(req: TemplateWriterReq) -> dict:
    """Co-author an input_template: send the current template + the user's instruction (+ the
    available {vars}) to the ``template_writer`` agent_server preset and return the improved
    template. Degrades loudly-but-gracefully (never 500s the studio)."""
    if not req.instruction.strip():
        return {"ok": False, "error": "instruction is required"}

    from .agent_server_client import AgentServerClient, AgentServerError

    user = (
        "CURRENT TEMPLATE:\n" + (req.template.strip() or "(empty)")
        + "\n\nVARIABLES: " + (", ".join(req.vars) if req.vars else "(none)")
        + "\n\nINSTRUCTION:\n" + req.instruction.strip()
    )
    client = AgentServerClient(settings.agent_server_url)
    try:
        msg = await client.chat(TEMPLATE_WRITER_PRESET, [{"role": "user", "content": user}])
    except AgentServerError as exc:
        log.warning("template-writer failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    content = _THINK_RE.sub("", msg.get("content") or "").strip()
    if not content:
        return {"ok": False, "error": "template_writer returned an empty template"}
    return {"ok": True, "template": content}


# --- bulk --------------------------------------------------------------------

@router.post("/reload")
async def reload(request: Request) -> dict:
    """Re-read all records from disk (authoritative reset of the registry)."""
    reg = _registry(request)
    reg.load_all()
    return {"ok": True, "agents": reg.ids}
