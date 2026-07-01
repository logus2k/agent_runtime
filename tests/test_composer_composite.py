"""Phase 4: Composite — a workflow-as-a-block (nesting).

Proves nesting executes: a Composite node runs its inner IRGraph in a nested executor,
so a workflow can participate inside another workflow, to any depth. Also checks the
Composite block's boundary schema + validation.
"""

from agent_runtime.composer import (
    Composite,
    ExecContext,
    GraphExecutor,
    IREdge,
    IRGraph,
    IRNode,
    composite_handler,
)
from agent_runtime.composer.schema import STRING


def _inner_graph() -> IRGraph:
    # inner: double -> plus1
    return IRGraph(
        nodes={"double": IRNode("double", "double"), "plus1": IRNode("plus1", "plus1")},
        edges=[IREdge("double", "plus1")],
        entry="double",
    )


async def test_composite_runs_inner_graph_within_outer():
    async def double(node, value, ctx):
        return value * 2

    async def plus1(node, value, ctx):
        return value + 1

    async def tag(node, value, ctx):
        return f"final:{value}"

    leaf = {"double": double, "plus1": plus1}
    outer = IRGraph(
        nodes={
            "sub": IRNode("sub", "composite", {"inner": _inner_graph()}),
            "end": IRNode("end", "tag"),
        },
        edges=[IREdge("sub", "end")],
        entry="sub",
    )
    handlers = {**leaf, "composite": composite_handler(leaf), "tag": tag}
    # 5 -> (double)10 -> (plus1)11 -> (tag) "final:11"
    out = await GraphExecutor(handlers).run(outer, 5)
    assert out == "final:11"


async def test_nesting_composes_to_depth_two():
    async def inc(node, value, ctx):
        return value + 1

    leaf = {"inc": inc}
    level1 = IRGraph(nodes={"i": IRNode("i", "inc")}, edges=[], entry="i")
    # a composite whose inner contains ANOTHER composite
    level2_inner = IRGraph(
        nodes={"c1": IRNode("c1", "composite", {"inner": level1})}, edges=[], entry="c1"
    )
    top = IRGraph(
        nodes={"c2": IRNode("c2", "composite", {"inner": level2_inner})}, edges=[], entry="c2"
    )
    handlers = {**leaf, "composite": composite_handler({**leaf})}
    # composite handler is shared, so it resolves nested composites too
    handlers["composite"] = composite_handler(handlers)
    out = await GraphExecutor(handlers).run(top, 0)
    assert out == 1  # one inc, reached through two composite layers


async def test_composite_block_schema_and_validation():
    c = Composite(inner=_inner_graph(), in_schema=STRING, out_schema=STRING)
    schema = c.get_schema()
    assert schema.category == "Composite"
    assert schema.port("in").schema == STRING
    assert c.validate() == []
    # no inner and no workflow_ref -> invalid
    assert Composite().validate()
    # workflow_ref alone is enough (a saved workflow the runtime resolves)
    assert Composite(config={"workflow_ref": "news-agent"}).validate() == []


async def test_composite_missing_inner_fails_loudly():
    from agent_runtime.composer import ExecutionError
    import pytest

    bad = IRGraph(nodes={"c": IRNode("c", "composite", {})}, edges=[], entry="c")
    with pytest.raises(ExecutionError, match="no inner graph"):
        await GraphExecutor({"composite": composite_handler({})}).run(bad, 0)
