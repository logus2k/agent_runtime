"""Composer API — the editor's contract endpoints.

These make the Block model authoritative for *clients*. Patron (one of possibly
several clients) renders its palette/ports/validation from ``GET /composer/catalog``
and lowers graphs via ``POST /composer/compile`` — so there is no JS copy of the
contract (the ``compile.js`` ↔ ``dsl.py`` duplication is dissolved).

  GET  /composer/catalog   -> { version, blocks: [ block-schema entry, ... ] }
  POST /composer/compile   <- a serialized composer graph (litegraph serialize() shape)
                            -> { ok, dsl, schedule } | { ok: false, errors: [...] }

Stateless and pure (no registry/bus/lifespan needed), so it is fully unit-testable
offline and cheap to serve. The compile result mirrors ``compile.js`` exactly, so a
client can swap its local compiler for this endpoint with no format change.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body

from .composer import Catalog, lower_graph
from .composer.lower import LoweringError

log = logging.getLogger("agent_runtime.composer_api")
router = APIRouter(prefix="/composer", tags=["composer"])


@router.get("/catalog")
async def get_catalog() -> dict[str, Any]:
    """The block-schema catalog the editor renders from (one source of truth)."""
    return Catalog().to_json()


@router.post("/compile")
async def compile_graph(graph: Any = Body(...)) -> dict[str, Any]:
    """Lower a serialized composer graph to the runtime DSL (link-traced).

    Returns 200 with ``{ok: false, errors}`` for an UNLOWERABLE-BUT-WELL-FORMED graph
    (validation errors are data the editor shows the human, not an HTTP failure), and
    ``{ok: false, errors}`` for a structurally invalid request body too — never a 500
    on bad input (no silent failures: the reason is always in ``errors``)."""
    try:
        return lower_graph(graph)
    except LoweringError as exc:
        return {"ok": False, "errors": [str(exc)]}
