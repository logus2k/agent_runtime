"""Phase 3: the graph-form IR + GraphExecutor.

Proves the graph is load-bearing at EXECUTION time (not just compile time): the next
node is decided by the chosen out-edge, so branching routes data-dependently and loops
repeat a bounded number of times. Handlers are fakes (no services) — the executor is
pure routing + safety.
"""

import copy

import pytest

from agent_runtime.composer import (
    Branch,
    ExecContext,
    ExecutionError,
    GraphExecutor,
    IREdge,
    IRGraph,
    IRNode,
    Loop,
    StepResult,
)
from agent_runtime.composer.lower import Graph

from _agentfixtures import agent_fixture_params

PAIRS = agent_fixture_params()


# --- linear execution ----------------------------------------------------------
async def test_linear_graph_executes_in_wired_order():
    order: list[str] = []

    async def rec(node, value, ctx):
        order.append(node.id)
        return f"{value}->{node.id}"

    g = IRGraph(
        nodes={"a": IRNode("a", "step"), "b": IRNode("b", "step"), "c": IRNode("c", "step")},
        edges=[IREdge("a", "b"), IREdge("b", "c")],
        entry="a",
    )
    out = await GraphExecutor({"step": rec}).run(g, "start")
    assert order == ["a", "b", "c"]
    assert out == "start->a->b->c"


# --- branching: the payoff (presence-based compilers can't do this) -------------
async def test_branch_routes_data_dependently():
    async def branch(node, value, ctx):
        # route on the input value, at run time
        return StepResult(value, port="then" if value > 0 else "else")

    async def mark(node, value, ctx):
        return node.id

    g = IRGraph(
        nodes={
            "br": IRNode("br", "branch"),
            "pos": IRNode("pos", "sink"),
            "neg": IRNode("neg", "sink"),
        },
        edges=[IREdge("br", "pos", port="then"), IREdge("br", "neg", port="else")],
        entry="br",
    )
    ex = GraphExecutor({"branch": branch, "sink": mark})
    assert await ex.run(g, 5) == "pos"
    assert await ex.run(g, -3) == "neg"


async def test_branch_with_no_matching_out_edge_fails_loudly():
    async def branch(node, value, ctx):
        return StepResult(value, port="missing")

    g = IRGraph(
        nodes={"br": IRNode("br", "branch"), "x": IRNode("x", "sink")},
        edges=[IREdge("br", "x", port="then")],
        entry="br",
    )
    async def sink(node, value, ctx):
        return value

    with pytest.raises(ExecutionError, match="no matching"):
        await GraphExecutor({"branch": branch, "sink": sink}).run(g, 1)


# --- loops: bounded repetition -------------------------------------------------
async def test_loop_repeats_then_exits():
    async def loop(node, value, ctx):
        # increment until 3, then exit; body routes back to the loop
        n = value + 1
        return StepResult(n, port="exit" if n >= 3 else "body")

    async def body(node, value, ctx):
        return value  # pass-through body that routes back to the loop

    async def done(node, value, ctx):
        return f"done:{value}"

    g = IRGraph(
        nodes={
            "lp": IRNode("lp", "loop"),
            "bd": IRNode("bd", "body"),
            "end": IRNode("end", "done"),
        },
        edges=[IREdge("lp", "bd", port="body"), IREdge("bd", "lp"), IREdge("lp", "end", port="exit")],
        entry="lp",
    )
    out = await GraphExecutor({"loop": loop, "body": body, "done": done}).run(g, 0)
    assert out == "done:3"


async def test_miswired_loop_hits_step_budget_not_infinite_hang():
    async def spin(node, value, ctx):
        return value  # always routes forward -> cycle with no exit

    g = IRGraph(
        nodes={"a": IRNode("a", "spin"), "b": IRNode("b", "spin")},
        edges=[IREdge("a", "b"), IREdge("b", "a")],
        entry="a",
    )
    with pytest.raises(ExecutionError, match="step budget"):
        await GraphExecutor({"spin": spin}, max_steps=50).run(g, 0)


async def test_unknown_kind_fails_loudly():
    g = IRGraph(nodes={"a": IRNode("a", "mystery")}, edges=[], entry="a")
    with pytest.raises(ExecutionError, match="no handler"):
        await GraphExecutor({}).run(g, 0)


# --- the linear News Agent, executed via IR ------------------------------------
@pytest.mark.parametrize("graph, golden", PAIRS)
async def test_agent_ir_executes_end_to_end_with_fakes(graph, golden):
    """A lowered agent graph runs through the executor: trigger → agent → destination.
    The destination channel + target come from the golden, not a hardcoded literal."""
    ir = Graph(copy.deepcopy(graph)).to_ir()
    assert ir.entry.startswith("trigger:")
    dest = golden["dsl"]["delivery"]  # {channel, target}
    seen: list[str] = []

    async def run_kind(node, value, ctx):
        seen.append(node.kind)
        if node.kind == "agent":
            return "AGENT_OUTPUT"
        if node.kind == dest["channel"]:
            return {"delivered_to": node.config.get("target"), "text": value}
        return value

    handlers = {"trigger": run_kind, "agent": run_kind, dest["channel"]: run_kind}
    out = await GraphExecutor(handlers).run(ir, "fire")
    assert seen == ["trigger", "agent", dest["channel"]]
    assert out == {"delivered_to": dest["target"], "text": "AGENT_OUTPUT"}


# --- Branch/Loop block schema + validation -------------------------------------
def test_branch_and_loop_blocks_expose_schema():
    assert Branch().get_schema().category == "Control"
    assert Loop().get_schema().category == "Control"
    assert any(c.key == "max_iter" for c in Loop().get_schema().config)


def test_loop_max_iter_validation():
    assert Loop(config={"max_iter": 0}).validate()
    assert Loop(config={"max_iter": 5}).validate() == []
