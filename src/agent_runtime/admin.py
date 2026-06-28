"""Admin API — deploy/reload agent records at runtime, no restart.

This is the runtime side of the Patron → agent_runtime bridge. Patron compiles a
graph to a runtime-DSL record and pushes it here; the record is validated, persisted
to ``data/agents/<id>.yaml``, and upserted into the LIVE registry the Farm reads —
so the agent runs with the new settings on its next trigger.

  GET  /admin/agents        : list known agent ids
  GET  /admin/agents/{id}    : fetch one record (as stored, defaults materialised)
  PUT  /admin/agents/{id}    : validate + persist + live-upsert a record (Deploy)
  POST /admin/reload         : reload every record from disk (drops in-memory-only ids)

The surface is localhost-bound (compose publishes 127.0.0.1:6817) and fronted +
auth-gated by nginx for any external access. If it is ever widened, add a shared
secret check here (e.g. an ADMIN_TOKEN bearer).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from .config import settings
from .dsl import AgentRecord
from .registry import Registry

log = logging.getLogger("agent_runtime.admin")
router = APIRouter(prefix="/admin", tags=["admin"])


def _registry(request: Request) -> Registry:
    reg = getattr(request.app.state, "registry", None)
    if reg is None:
        raise HTTPException(status_code=503, detail="registry not ready")
    return reg


def _agents_dir(request: Request) -> Path:
    # Set on app.state in the lifespan; fall back to settings for tests/bare apps.
    return Path(getattr(request.app.state, "agents_dir", settings.agents_dir))


def _write_record_yaml(agents_dir: Path, agent_id: str, data: dict[str, Any]) -> Path:
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / f"{agent_id}.yaml"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    tmp.replace(path)  # atomic
    return path


@router.get("/agents")
async def list_agents(request: Request) -> dict:
    return {"agents": _registry(request).ids}


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, request: Request) -> dict:
    rec = _registry(request).get(agent_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"no agent '{agent_id}'")
    return rec.model_dump(mode="json", exclude_none=True)


@router.put("/agents/{agent_id}")
async def deploy_agent(agent_id: str, body: dict, request: Request) -> dict:
    """Validate a runtime-DSL record, persist it, and make it live."""
    try:
        record = AgentRecord.model_validate(body)
    except ValidationError as exc:
        # Loud + structured: the same validation the loader applies at boot.
        raise HTTPException(status_code=422, detail=f"invalid agent record: {exc}")
    if record.id != agent_id:
        raise HTTPException(
            status_code=400,
            detail=f"path id '{agent_id}' != record id '{record.id}'",
        )
    data = record.model_dump(mode="json", exclude_none=True)
    path = _write_record_yaml(_agents_dir(request), agent_id, data)
    _registry(request).upsert(record)
    log.info("deployed agent '%s' -> %s (live)", agent_id, path)
    return {"ok": True, "id": agent_id, "path": str(path), "live": True}


@router.post("/reload")
async def reload(request: Request) -> dict:
    """Re-read all records from disk (authoritative reset of the registry)."""
    reg = _registry(request)
    reg.load_all()
    return {"ok": True, "agents": reg.ids}
