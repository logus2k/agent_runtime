"""RAG node tests (block_management.md §8.1): retrieve-then-inject appends evidence to
the task; graceful degradation (down backend / no evidence / no domains) passes the task
through unchanged. Retrieval is mocked via an httpx ASGI-free transport, so no live
noted-rag / noted-graph is needed."""

from __future__ import annotations

import httpx
import pytest

from agent_runtime.config import Settings
from agent_runtime.dsl import Rag
from agent_runtime.nodes.rag import build_evidence, retrieve_and_inject


def _settings() -> Settings:
    return Settings()


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- build_evidence ---------------------------------------------------------

def test_build_evidence_formats_chunks_and_graph():
    chunks = [{"source_path": "doc.md", "section_path": "Intro", "text": "A fact."}]
    graph = {
        "entities": [
            {"id": "e1", "label": "Widget", "type": "product",
             "properties": {"description": "a thing"}}
        ],
        "edges": [{"source": "e1", "type": "made_by", "target": "e2"}],
    }
    ev = build_evidence(chunks, graph)
    assert "A fact." in ev
    assert "doc.md" in ev
    assert "Widget (product)" in ev
    assert "made_by" in ev


def test_build_evidence_empty_when_nothing():
    assert build_evidence([], {}) == ""


# --- retrieve_and_inject ----------------------------------------------------

# The migrated backend: embeddings-server /embed + ArcadeDB /api/v1/query/<db>
# + embeddings-server /v1/rerank. These mocks stand in for that fleet.
def _embed_resp() -> httpx.Response:
    return httpx.Response(200, json={"vectors": [[0.1] * 8],
                                     "sparse": [{"indices": [1], "weights": [0.5]}]})


async def test_injects_retrieved_context():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/embed"):
            return _embed_resp()
        if "/api/v1/query/" in path:   # ArcadeDB hybrid search over Chunk
            return httpx.Response(200, json={"result": [
                {"id": "cv#0", "text": "Injected fact.", "source_path": "s.md"}]})
        if path.endswith("/v1/rerank"):
            return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.9}]})
        raise AssertionError(f"unexpected call: {request.url}")

    rag = Rag(domains=["cv"], use_graph=False)  # no rewriter -> raw query
    async with _client(handler) as c:
        out = await retrieve_and_inject(rag, "the question", settings=_settings(), client=c)

    assert out.startswith("the question")
    assert "Injected fact." in out
    assert "Retrieved material you may use" in out


async def test_uses_graph_when_enabled():
    seen = {"search": False, "graph": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/embed"):
            return _embed_resp()
        if path.endswith("/v1/rerank"):
            return httpx.Response(200, json={"results": []})
        if "/api/v1/query/" in path:
            # The graph query selects Entity vertices; the corpus query selects Chunk.
            body = request.content.decode()
            if "Entity" in body:
                seen["graph"] = True
                return httpx.Response(200, json={"result": [
                    {"id": "e1", "label": "Node", "type": "x", "properties_json": "{}"}]})
            seen["search"] = True
            return httpx.Response(200, json={"result": []})
        raise AssertionError(f"unexpected: {request.url}")

    rag = Rag(domains=["kb"], use_graph=True)
    async with _client(handler) as c:
        out = await retrieve_and_inject(rag, "q", settings=_settings(), client=c)
    assert seen == {"search": True, "graph": True}
    assert "Node (x)" in out


async def test_passthrough_when_no_domains():
    rag = Rag(domains=[])
    # No client needed; the node short-circuits before any HTTP.
    out = await retrieve_and_inject(rag, "unchanged task", settings=_settings())
    assert out == "unchanged task"


async def test_degrades_gracefully_when_backend_down():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("noted-rag unreachable")

    rag = Rag(domains=["cv"])
    async with _client(handler) as c:
        out = await retrieve_and_inject(rag, "still here", settings=_settings(), client=c)
    # backend down -> passes the task through unchanged, no crash
    assert out == "still here"


async def test_passthrough_when_no_evidence():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"chunks": []})

    rag = Rag(domains=["cv"])
    async with _client(handler) as c:
        out = await retrieve_and_inject(rag, "task", settings=_settings(), client=c)
    assert out == "task"


async def test_rewriter_skip_short_circuits():
    calls = {"search": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "SKIP"}}]}
            )
        if request.url.path == "/search":
            calls["search"] += 1
            return httpx.Response(200, json={"chunks": [{"text": "x"}]})
        raise AssertionError(request.url)

    rag = Rag(domains=["cv"], rewriter="cv_query_rewriter")
    async with _client(handler) as c:
        out = await retrieve_and_inject(rag, "hello", settings=_settings(), client=c)
    assert out == "hello"
    assert calls["search"] == 0  # SKIP means no retrieval at all
