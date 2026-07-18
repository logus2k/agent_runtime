"""Ingestion node — a client of the Ingestion Agent (``ingestion_server``).

Holds no ingestion logic. The Agent owns docling, embeddings, extraction and the
graph; this reaches it over HTTP, the same way ``brain`` reaches agent_server and
``tools`` reaches MCP.

Two things it must get right, and both come from how folder_watch fires:

1. **The document is in the CONTEXT, not the input value.** folder_watch seeds
   ``data.task`` with the file's *content*, and its reader cannot decode a PDF —
   it returns ``[binary file …: N bytes, not UTF-8 text]``. The path lives in
   ``payload.context.file_path``. With the File Initiator's ``emit: path`` the
   seed IS the path, but context stays authoritative.
2. **A delete has no content to read**, so its seed is a path that is
   indistinguishable in shape from ``emit: path``. Only ``context.change`` says
   which it is. A node that trusts the seed alone would happily index the literal
   string ``/watched/in/foo.pdf`` as a document.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

log = logging.getLogger("agent_runtime.nodes.ingestion")

_DEFAULT_AGENT = "http://ingestion-server:8700"


def document_from_event(value: Any, context: dict | None) -> dict:
    """Build the Agent's document descriptor from the fired event.

    Context wins over the seed: on a delete there is no content, and on a binary
    create the seed is a marker string, not the file.
    """
    ctx = context or {}
    path = str(ctx.get("file_path") or "").strip()
    change = str(ctx.get("change") or "").strip() or "created"

    if not path:
        # No file provenance — the node was fired by something other than a File
        # Initiator (a Web request, a manual run). Fall back to the flow value,
        # which is then expected to BE a path.
        path = str(value or "").strip()
        change = "created"

    if not path:
        raise ValueError(
            "ingestion: no document. Wire a File Initiator into this block "
            "(context.file_path), or pass a path as the incoming value.")

    doc: dict[str, Any] = {"path": path, "change": change}
    name = ctx.get("name") or path.rsplit("/", 1)[-1]
    if name:
        doc["name"] = name
    return doc


async def run_ingestion(node_config: dict, value: Any, context: dict | None,
                        timeout_s: float) -> dict:
    """Drive one ingest and return the finished run.

    Blocks until the Agent is done (``wait=true``) so the workflow's next activity
    sees a real outcome rather than a run id. That is why per-node ``timeout_s``
    exists: an ingest is ~100s per document and the farm's global cap is 120s.
    """
    cfg = node_config or {}
    base = str(cfg.get("agent_url") or _DEFAULT_AGENT).rstrip("/")
    pipeline = cfg.get("pipeline")
    if isinstance(pipeline, str):
        pipeline = json.loads(pipeline)
    if not pipeline:
        raise ValueError("ingestion: no pipeline configured")

    body: dict[str, Any] = {
        "pipeline": pipeline,
        "documents": [document_from_event(value, context)],
        "wait": True,
    }
    judge = cfg.get("judge")
    if judge and judge.get("enabled", True):
        body["judge"] = {k: v for k, v in judge.items() if k != "enabled"}

    # Leave the Agent a little less than the node's own bound, so IT reports a
    # timeout with a real run id rather than the node being killed blind.
    http_timeout = max(10.0, timeout_s - 5.0) if timeout_s else 1800.0
    async with httpx.AsyncClient(timeout=http_timeout) as c:
        r = await c.post(f"{base}/v1/runs", json=body)
        if r.status_code == 422:
            raise ValueError(f"ingestion: the Agent rejected the pipeline: "
                             f"{r.text[:300]}")
        if r.status_code >= 400:
            raise RuntimeError(f"ingestion: agent HTTP {r.status_code}: {r.text[:300]}")
        return r.json()


def summarise(run: dict) -> dict:
    """What the node emits downstream and what it logs — the outcome, not the raw
    run. `DataSchema` has no array type, so the full run travels as JSON text."""
    committed: dict[str, int] = {}
    deleted: dict[str, int] = {}
    flags: list[dict] = []
    for rep in run.get("reports") or []:
        for k, v in (rep.get("committed") or {}).items():
            committed[k] = committed.get(k, 0) + v
        for k, v in (rep.get("deleted") or {}).items():
            deleted[k] = deleted.get(k, 0) + v
        for L in rep.get("layers") or []:
            j = L.get("judge") or {}
            if j and not j.get("ok", True):
                flags.append({"layer": L.get("name"), "suspicion": j.get("suspicion"),
                              "note": j.get("note")})
    return {"run_id": run.get("run_id"), "state": run.get("state"),
            "committed": committed, "deleted": deleted, "flags": flags,
            "error": run.get("error")}
