"""Resources API — the management-plane contract, symmetric to ``/composer/catalog``.

  GET /resources/catalog     -> { version, resources: [ descriptor(json), … ] }
  GET /resources/{id}        -> { ok, items: [ … ], error }   (the list/pick source)

The editor renders the generic Resource Picker + Manager entirely from these — declaring a
resource is a Python change only (registry.py), no per-resource UI. See
``documents/resource_model.md``. Write verbs (create/update/delete/actions) are a later slice.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException, Request

from .resource import act_item, descriptor_by_id, descriptors_json, list_items, update_item

log = logging.getLogger("agent_runtime.resources")
router = APIRouter(prefix="/resources", tags=["resources"])


@router.get("/catalog")
async def catalog() -> dict:
    """The declared resource descriptors (source · capabilities · schema · actions)."""
    return descriptors_json()


@router.get("/{rid}")
async def list_resource(rid: str, request: Request) -> dict:
    """List one resource's items (its list/pick source). 404 if the id isn't declared."""
    desc = descriptor_by_id(rid)
    if desc is None:
        raise HTTPException(status_code=404, detail=f"no resource '{rid}'")
    return await list_items(desc, request)


@router.put("/{rid}/{key}")
async def update_resource(rid: str, key: str, request: Request, body: dict = Body(default={})) -> dict:
    """Update one item from a schema-form body — gated by the `update` capability + `editable`."""
    desc = descriptor_by_id(rid)
    if desc is None:
        raise HTTPException(status_code=404, detail=f"no resource '{rid}'")
    if "update" not in desc.capabilities or not desc.editable:
        raise HTTPException(status_code=405, detail=f"'{rid}' is not editable")
    return await update_item(desc, request, key, body)


@router.delete("/{rid}/{key}")
async def delete_resource(rid: str, key: str, request: Request) -> dict:
    """Delete one item — gated by the descriptor's `delete` capability."""
    desc = descriptor_by_id(rid)
    if desc is None:
        raise HTTPException(status_code=404, detail=f"no resource '{rid}'")
    if "delete" not in desc.capabilities:
        raise HTTPException(status_code=405, detail=f"'{rid}' is not deletable")
    return await act_item(desc, request, key, "delete")


@router.post("/{rid}/{key}/{verb}")
async def action_resource(rid: str, key: str, verb: str, request: Request) -> dict:
    """Run a declared action verb (pause/resume/run/…) on one item — gated by the descriptor's
    `actions`, so arbitrary verbs can't be forwarded to a backing service."""
    desc = descriptor_by_id(rid)
    if desc is None:
        raise HTTPException(status_code=404, detail=f"no resource '{rid}'")
    if verb not in desc.actions:
        raise HTTPException(status_code=405, detail=f"'{rid}' has no action '{verb}'")
    return await act_item(desc, request, key, verb)
