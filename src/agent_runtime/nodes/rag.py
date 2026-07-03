"""The RAG node — pre-inference retrieve-then-inject (block_management.md §8.1).

A RAG block is wired BEFORE an Agent: it retrieves context for the incoming task and
**injects it into the value** that flows to the downstream Agent (whose input template /
pass-through then carries it into the brain). This ports cv/backend/main.py's proven
retrieve-then-inject: optionally reformulate the query via an agent_server rewriter
preset, fan out to the dense corpus (noted-rag) and, when enabled, the knowledge graph
(noted-graph), format one evidence block, and append it to the task.

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


async def _search_corpus(
    client: httpx.AsyncClient, noted_rag_url: str, query: str, domain: str, top_k: int,
    rerank_min_score: float = 0.0,
) -> list[dict]:
    """Dense-corpus retrieval from noted-rag for one domain's ``<domain>__corpus``
    collection (the convention cv/backend uses). ``rerank_min_score`` MUST be sent —
    without it noted-rag's reranker applies its own default threshold and can drop every
    chunk (0 results on a populated corpus). Fails soft to []."""
    try:
        r = await client.post(
            f"{noted_rag_url.rstrip('/')}/search",
            json={
                "query": query,
                "collection": f"{domain}__corpus",
                "top_k": top_k,
                "rerank_min_score": rerank_min_score,
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("chunks") or []
    except Exception as exc:  # noqa: BLE001 - one domain down must not fail the whole node
        log.warning("rag corpus search failed (domain=%s): %s", domain, exc)
        return []


async def _search_graph(
    client: httpx.AsyncClient, noted_graph_url: str, question: str, domain: str
) -> dict:
    """Knowledge-graph retrieval from noted-graph (local mode). Fails soft to {}."""
    try:
        r = await client.post(
            f"{noted_graph_url.rstrip('/')}/research/{domain}/retrieve",
            json={"question": question, "mode": "local"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json() or {}
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
            client, settings.noted_rag_url, query, domain, top_k, settings.rag_rerank_min_score
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
        graph = await _search_graph(client, settings.noted_graph_url, query, domain)
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
                client, settings.noted_rag_url, query, domain, settings.rag_top_k,
                settings.rag_rerank_min_score,
            )
            all_chunks.extend(chunks)
            if rag.use_graph:
                g = await _search_graph(
                    client, settings.noted_graph_url, text, domain
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
