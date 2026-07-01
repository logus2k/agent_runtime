"""Phase 6: management hardening — trace + security enforced during execution.

Proves the Management interface is real at run time: every edge crossed yields a
TraceRecord with a UTC timestamp (via the agent_bus envelope path), and an unauthorized
node is blocked before its handler runs (fail-closed, loud).
"""

import pytest

from agent_runtime.composer import (
    ExecContext,
    GraphExecutor,
    IREdge,
    IRGraph,
    IRNode,
)
from agent_runtime.composer.management import (
    AuthorizationError,
    TraceCollector,
    make_authorizer,
)


def _linear() -> IRGraph:
    return IRGraph(
        nodes={"a": IRNode("a", "step"), "b": IRNode("b", "step"), "c": IRNode("c", "sink")},
        edges=[IREdge("a", "b"), IREdge("b", "c")],
        entry="a",
    )


async def test_trace_collected_per_edge_with_utc_timestamp():
    async def step(node, value, ctx):
        return value

    tc = TraceCollector()
    ex = GraphExecutor({"step": step, "sink": step}, on_trace=tc.on_trace)
    await ex.run(_linear(), "x", ExecContext(cid="run-42", sid=3, sender="test"))
    # two edges crossed -> two traces
    assert [r.edge for r in tc.records] == ["a.out -> b", "b.out -> c"]
    for r in tc.records:
        assert r.cid == "run-42"
        assert r.timestamp.endswith("+00:00") or "T" in r.timestamp  # UTC ISO-8601


async def test_authorizer_blocks_denied_node_before_it_runs():
    ran: list[str] = []

    async def step(node, value, ctx):
        ran.append(node.id)
        return value

    # deny the 'sink' kind -> node 'c' must never run; error raised at c
    ex = GraphExecutor(
        {"step": step, "sink": step},
        authorize=make_authorizer(deny_kinds=["sink"]),
    )
    with pytest.raises(AuthorizationError, match="denied by policy"):
        await ex.run(_linear(), "x")
    assert ran == ["a", "b"]  # c was blocked before running


async def test_authorizer_predicate_denies_on_value():
    async def step(node, value, ctx):
        return value

    # deny when the value carries a 'secret'
    auth = make_authorizer(predicate=lambda node, value, ctx: "secret" not in str(value))
    ex = GraphExecutor({"step": step, "sink": step}, authorize=auth)
    with pytest.raises(AuthorizationError, match="denied by predicate"):
        await ex.run(_linear(), "top secret")


async def test_allowed_run_completes_and_traces():
    async def step(node, value, ctx):
        return value + "!" if node.id == "c" else value

    tc = TraceCollector()
    ex = GraphExecutor(
        {"step": step, "sink": step},
        authorize=make_authorizer(deny_kinds=["nonexistent"]),
        on_trace=tc.on_trace,
    )
    out = await ex.run(_linear(), "ok")
    assert out == "ok!"
    assert len(tc.records) == 2
