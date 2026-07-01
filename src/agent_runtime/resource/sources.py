"""Resource sources — the ``list()`` side of each resource, bound to its backing service.

One dispatch keyed by ``descriptor.source``. Server-reachable sources reuse the existing
admin fetchers / registry / scheduler; the ``client`` source (recipes) is owned by the
editor's own server (patron serve.py), so the runtime reports it as client-owned.

Every source degrades loudly-but-gracefully (never 500s the editor) and returns a uniform
envelope: ``{ok, items, error}``.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import Request

from ..config import settings
from .descriptor import ResourceDescriptor


async def list_items(desc: ResourceDescriptor, request: Request) -> dict[str, Any]:
    """List a resource's items as ``{ok, items:[{…}], error}`` — reuses the SDK-backed
    fetchers that already exist for the bespoke pickers (which this generalizes)."""
    src = desc.source
    try:
        if src == "mcp":
            from ..admin import _fetch_mcp_tools
            d = await _fetch_mcp_tools()
            return {"ok": bool(d.get("server_ok")), "items": d.get("tools", []), "error": d.get("error")}

        if src == "agent_server":
            from ..admin import _fetch_presets
            d = await _fetch_presets()
            return {"ok": bool(d.get("server_ok")), "items": d.get("presets", []), "error": d.get("error")}

        if src == "whatsapp":
            from ..admin import _fetch_whatsapp_targets
            d = await _fetch_whatsapp_targets()
            return {"ok": bool(d.get("bridge_ok")), "items": d.get("targets", []), "error": d.get("error")}

        if src == "runtime":  # agents, from the live registry
            from ..admin import _registry, _summary
            reg = _registry(request)
            return {"ok": True, "items": [_summary(r) for r in reg.all()], "error": None}

        if src == "scheduler":  # triggers == scheduler jobs
            url = f"{settings.scheduler_url.rstrip('/')}/jobs"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("jobs", data) if isinstance(data, dict) else data
            return {"ok": True, "items": items, "error": None}

        if src == "client":  # patron-local (recipes): the runtime does not own the store
            return {"ok": False, "items": [],
                    "error": f"'{desc.id}' is a client-local resource; list it from the editor's server"}

    except Exception as exc:  # noqa: BLE001 - surface loudly, don't 500 the editor
        return {"ok": False, "items": [], "error": str(exc)}

    return {"ok": False, "items": [], "error": f"unknown source '{src}'"}


async def act_item(desc: ResourceDescriptor, request: Request, key: str, verb: str) -> dict[str, Any]:
    """Perform a verb ('delete' or a declared action like pause/resume/run) on one item,
    routed to the backing service. Returns ``{ok, status, error}``. The caller (resources_api)
    has already gated the verb against the descriptor's capabilities/actions."""
    src = desc.source
    try:
        if src == "scheduler":  # trigger jobs
            base = f"{settings.scheduler_url.rstrip('/')}/jobs/{key}"
            method, url = ("DELETE", base) if verb == "delete" else ("POST", f"{base}/{verb}")
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.request(method, url)
            ok = resp.status_code < 300
            return {"ok": ok, "status": resp.status_code, "error": None if ok else resp.text[:300]}

        if src == "runtime":  # agents
            if verb == "delete":
                from ..admin import delete_agent
                await delete_agent(key, request)  # 204 or raises HTTPException
                return {"ok": True, "status": 204, "error": None}
            return {"ok": False, "error": f"action '{verb}' not implemented yet for '{desc.id}'"}

        return {"ok": False, "error": f"'{desc.id}' ({src}) has no write support yet"}

    except Exception as exc:  # noqa: BLE001 - surface the failure, don't 500
        detail = getattr(exc, "detail", None)
        return {"ok": False, "error": str(detail if detail is not None else exc)}
