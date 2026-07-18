"""The RAG node — pre-inference retrieve-then-inject (block_management.md §8.1).

A RAG block is wired BEFORE an Agent: it retrieves context for the incoming task and
**injects it into the value** that flows to the downstream Agent (whose input template /
pass-through then carries it into the brain). This ports cv/backend/main.py's proven
retrieve-then-inject: optionally reformulate the query via an agent_server rewriter
preset, fan out to the dense corpus and, when enabled, the knowledge graph (both on
graph-server-arcadedb + embeddings-server), format one evidence block, and append it to the task.

Graceful degradation is mandatory (§ task): if the retrieval backend is unreachable, the
node **passes the value through unchanged** and logs a LOUD warning — it never crashes the
run. A down service yields empty evidence, never a 500.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import Settings
from ..dsl import Rag

log = logging.getLogger("agent_runtime.rag")

# The banner separating the original task from the injected evidence (mirrors cv/backend).
_EVIDENCE_HEADER = (
    "\n\n---\nRetrieved material you may use to answer (cite where relevant):\n\n"
)


async def _formulate_query(
    client: httpx.AsyncClient, agent_server_url: str, rewriter: str, message: str
) -> str:
    """Reformulate the task into a focused retrieval query via a small LLM pass (the
    same trick cv/backend uses so the cross-encoder reranker scores well). Fails SOFT:
    any error falls back to the raw message. Empty/``SKIP`` -> '' (skip retrieval)."""
    try:
        r = await client.post(
            f"{agent_server_url.rstrip('/')}/v1/chat/completions",
            json={
                "model": rewriter,
                "messages": [{"role": "user", "content": message}],
                "stream": False,
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        q = (
            (data.get("choices") or [{}])[0]
            .get("message", {})
            .get("content", "")
        ).strip()
        q = q.strip().strip('"').strip("'").splitlines()[0].strip() if q else ""
        if q.upper() == "SKIP":
            return ""
        return q or message
    except Exception as exc:  # noqa: BLE001 - fail soft, never crash the run
        log.warning("rag rewriter '%s' failed, using raw query: %s", rewriter, exc)
        return message


async def _arcade_query(client: httpx.AsyncClient, settings: Settings, domain: str,
                        command: str, params: dict) -> list[dict]:
    """One ArcadeDB SQL query against the ``domain`` database. Fails soft to []."""
    r = await client.post(
        f"{settings.arcadedb_url.rstrip('/')}/api/v1/query/{domain}",
        auth=(settings.arcadedb_user, settings.arcadedb_password),
        json={"language": "sql", "command": command, "params": params},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("result") or []


async def _search_corpus(
    client: httpx.AsyncClient, settings: Settings, query: str, domain: str, top_k: int,
    rerank_min_score: float = 0.0,
) -> list[dict]:
    """Hybrid dense+sparse retrieval from graph-server-arcadedb for the ``domain``
    database's ``Chunk`` type, then cross-encoder rerank via embeddings-server. A
    port of cv/backend's proven ``_arcade_search``: dense (bge-m3) + learned-sparse
    fused with RRF, reranked, trimmed to top_k. Falls back to dense-only if the
    sparse leg fails. Fails soft to []."""
    try:
        emb_url = settings.embed_url.rstrip("/")
        dense = (await client.post(f"{emb_url}/embed", json={"texts": [query]},
                                   timeout=30)).json()["vectors"][0]
        qi = qw = None
        try:
            sp = (await client.post(f"{emb_url}/embed",
                    json={"texts": [query], "dense": False, "sparse": True},
                    timeout=30)).json().get("sparse") or []
            if sp and sp[0].get("indices"):
                qi, qw = sp[0]["indices"], sp[0]["weights"]
        except Exception:  # noqa: BLE001 - dense-only is a valid degrade
            qi = qw = None

        fields = ("SELECT chunk_id AS id, text, source_path, section_path, page_no "
                  "FROM (SELECT expand(")
        cand = settings.rag_arcade_candidates
        if qi:
            command = (fields + "vector.fuse("
                       "vector.neighbors('Chunk[embedding]', :v, :k),"
                       "vector.sparseNeighbors('Chunk[sidx,swt]', :qi, :qw, :k),"
                       "{'fusion':'RRF'})))")
            params = {"v": dense, "qi": qi, "qw": qw, "k": cand}
        else:
            command = fields + "vector.neighbors('Chunk[embedding]', :v, :k)))"
            params = {"v": dense, "k": cand}
        rows = await _arcade_query(client, settings, domain, command, params)
        if not rows:
            return []

        rr = await client.post(f"{settings.rerank_url.rstrip('/')}/v1/rerank",
                               json={"model": settings.rerank_model, "query": query,
                                     "documents": [x.get("text") or "" for x in rows]},
                               timeout=30)
        rr.raise_for_status()
        res = rr.json().get("results") or []
        ordered = sorted(res, key=lambda x: -float(x["relevance_score"]))
        out = []
        for x in ordered:
            if float(x["relevance_score"]) < rerank_min_score:
                continue
            i = int(x["index"])
            if 0 <= i < len(rows):
                out.append(rows[i])
            if len(out) >= top_k:
                break
        return out
    except Exception as exc:  # noqa: BLE001 - one domain down must not fail the whole node
        log.warning("rag corpus search failed (domain=%s): %s", domain, exc)
        return []


async def _search_graph(
    client: httpx.AsyncClient, settings: Settings, question: str, domain: str
) -> dict:
    """Knowledge-graph retrieval from graph-server-arcadedb: embed the question, find
    the nearest ``Entity`` vertices, and return them plus their RELATES edges. A port
    of cv/backend's ``_arcade_graph``. Fails soft to {}."""
    try:
        emb = (await client.post(f"{settings.embed_url.rstrip('/')}/embed",
                                 json={"texts": [question]}, timeout=30)).json()["vectors"][0]

        def _props(s):
            try:
                import json as _json
                return _json.loads(s.get("properties_json") or "{}")
            except Exception:  # noqa: BLE001
                return {}

        seeds = await _arcade_query(
            client, settings, domain,
            "SELECT id, label, type, properties_json FROM "
            "(SELECT expand(vector.neighbors('Entity[embedding]', :v, 7)))", {"v": emb})
        if not seeds:
            return {}
        ids = [s["id"] for s in seeds]
        entities = [{"id": s["id"], "label": s.get("label"), "type": s.get("type"),
                     "properties": _props(s)} for s in seeds]
        edges = []
        seen = set()
        for e in await _arcade_query(
                client, settings, domain,
                "SELECT outV().id AS source, inV().id AS target, type "
                "FROM (SELECT expand(bothE('RELATES')) FROM Entity WHERE id IN :ids) "
                "LIMIT 30", {"ids": ids}):
            key = (e.get("source"), e.get("target"), e.get("type"))
            if key not in seen:
                seen.add(key)
                edges.append({"source": e.get("source"), "target": e.get("target"),
                              "type": e.get("type")})
        return {"entities": entities, "edges": edges}
    except Exception as exc:  # noqa: BLE001
        log.warning("rag graph retrieval failed (domain=%s): %s", domain, exc)
        return {}


def build_evidence(chunks: list[dict], graph: dict) -> str:
    """Format retrieved corpus chunks + graph context into one evidence block (a trimmed
    port of cv/backend's ``_build_evidence`` — same shape, no citation-tag machinery)."""
    parts: list[str] = []
    if chunks:
        parts.append("## Documentation chunks (most relevant passages)")
        for c in chunks:
            src = c.get("source_path") or ""
            parts.append(f"### source: {src}")
            if c.get("section_path"):
                parts.append(f"_section: {c['section_path']}_")
            parts.append((c.get("text") or "").strip())
            parts.append("")

    entities = graph.get("entities") or []
    edges = graph.get("edges") or []
    if entities or edges:
        parts.append("## Knowledge-graph context")
        for e in entities[:25]:
            label = e.get("label") or e.get("id")
            etype = e.get("type", "")
            desc = ((e.get("properties") or {}).get("description") or "").strip()
            line = f"- {label} ({etype})"
            if desc:
                line += f" — {desc}"
            parts.append(line)
        for ed in edges[:25]:
            parts.append(
                f"- {ed.get('source')} >{ed.get('type')}> {ed.get('target')}"
            )
        parts.append("")

    return "\n".join(parts).strip()


async def query_vector(
    *, query: str, domain: str, top_k: int, settings: Settings,
    client: "httpx.AsyncClient | None" = None,
) -> str:
    """Standalone **Vector Database** query (§ retriever block): dense-corpus retrieval from
    noted-rag for ``domain``, formatted as an evidence block and RETURNED as the flow value
    (unlike RAG-pre, which injects into an Agent's task). Empty query/domain → "". Fails soft."""
    if not query.strip() or not domain.strip():
        return ""
    own = client is None
    client = client or httpx.AsyncClient()
    try:
        chunks = await _search_corpus(
            client, settings, query, domain, top_k, settings.rag_rerank_min_score
        )
    finally:
        if own:
            await client.aclose()
    return build_evidence(chunks, {"entities": [], "edges": []})


async def query_graph(
    *, query: str, domain: str, settings: Settings,
    client: "httpx.AsyncClient | None" = None,
) -> str:
    """Standalone **Graph Database** query: knowledge-graph retrieval from noted-graph for
    ``domain``, formatted and RETURNED as the flow value. Empty query/domain → "". Fails soft."""
    if not query.strip() or not domain.strip():
        return ""
    own = client is None
    client = client or httpx.AsyncClient()
    try:
        graph = await _search_graph(client, settings, query, domain)
    finally:
        if own:
            await client.aclose()
    return build_evidence([], graph)


async def retrieve_and_inject(
    rag: Rag,
    value: Any,
    *,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Retrieve context for ``value`` and inject it, returning the augmented task string.

    Reads the RAG node's config (``rewriter`` / ``domains`` / ``use_graph``). With no
    domains configured, retrieval is a no-op and the value passes through unchanged.
    On ANY unexpected failure the value passes through and a loud warning is logged —
    the run is never crashed by a down retrieval backend."""
    text = "" if value is None else str(value)
    if not rag.domains:
        return text

    own_client = client is None
    client = client or httpx.AsyncClient()
    try:
        query = text
        if rag.rewriter:
            query = await _formulate_query(
                client, settings.agent_server_url, rag.rewriter, text
            )
        if not query:  # rewriter said SKIP — no retrieval, pass through
            return text

        all_chunks: list[dict] = []
        merged_graph: dict = {"entities": [], "edges": []}
        for domain in rag.domains:
            chunks = await _search_corpus(
                client, settings, query, domain, settings.rag_top_k,
                settings.rag_rerank_min_score,
            )
            all_chunks.extend(chunks)
            if rag.use_graph:
                g = await _search_graph(
                    client, settings, text, domain
                )
                merged_graph["entities"].extend(g.get("entities") or [])
                merged_graph["edges"].extend(g.get("edges") or [])

        evidence = build_evidence(all_chunks, merged_graph)
        if not evidence:
            log.warning(
                "rag retrieved no evidence for domains %s — passing task through unchanged",
                rag.domains,
            )
            return text
        return f"{text}{_EVIDENCE_HEADER}{evidence}"
    except Exception as exc:  # noqa: BLE001 - degrade gracefully, never crash the run
        log.warning(
            "rag node failed (%s: %s) — passing task through unchanged", type(exc).__name__, exc
        )
        return text
    finally:
        if own_client:
            await client.aclose()
