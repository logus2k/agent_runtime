"""Resources API — the management-plane contract, symmetric to ``/composer/catalog``.

  GET /resources/catalog     -> { version, resources: [ descriptor(json), … ] }
  GET /resources/{id}        -> { ok, items: [ … ], error }   (the list/pick source)

The editor renders the generic Resource Picker + Manager entirely from these — declaring a
resource is a Python change only (registry.py), no per-resource UI. See
``documents/resource_model.md``. Write verbs (create/update/delete/actions) are a later slice.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from .resource import descriptor_by_id, descriptors_json, list_items

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
