"""Phase 04 — the persisted graph/workflow record + graph executor + decomposition.

Covers the phase's exit criteria (implementation_plan/04):
  * a 3-node linear graph runs like today;
  * a fan-out node delivers to ALL successors;
  * a fan-in node runs once per incoming message (no barrier);
  * decomposed RAG/guardrail nodes execute in order;
  * idempotent upsert updates in place + bumps version;
  * a legacy flat record loads via the compat shim and runs unchanged.

All fakes; no live services (mirrors test_runner_graph.py / test_workflow_multi_agent.py).
"""

import dataclasses

import pytest
from agent_bus_client import new_event

from agent_runtime.config import Settings
from agent_runtime.dsl import (
    AgentRecord, Brain, Delivery, Guardrails, Input, Rag,
)
from agent_runtime.dsl_graph import (
    GraphEdge, GraphNode, GraphRecord, from_flat_record,
)
from agent_runtime.graph_executor import (
    GraphExecutionError, GraphWorkflowExecutor, WalkContext,
)
from agent_runtime.graph_registry import GraphRegistry
from agent_runtime.runner import Runner


# --- Fakes (mirror the other runner tests) -------------------------------------
class FakeAgentServer:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []

    async def chat(self, model, messages, *, tools=None, overrides=None):
        self.calls.append({"model": model, "messages": list(messages), "tools": tools})
        # Cycle through scripted responses; if only one, reuse it (fan-out agents).
        if len(self._scripted) > 1:
            return self._scripted.pop(0)
        return self._scripted[0]


class FakeBus:
    def __init__(self):
        self.published = []
        self._n = 0

    def stream_key(self, sid):
        return f"stream:{sid}"

    async def incr(self, key):
        self._n += 1
        return self._n

    async def expire(self, key, ttl):
        return True

    async def publish(self, stream, env):
        self.published.append((stream, env))
        return "1-0"


def _settings():
    return dataclasses.replace(
        Settings(), runs_stream_id="runs", sender_id="agent-runtime",
    )


def _flat_record(**kw) -> AgentRecord:
    base = dict(
        version="0.1",
        uid="00000000-0000-4000-8000-00000000ca11",
        name="news-morning-ai",
        brain=Brain(persona="news_curator"),
        input=Input(template="Curate {topic}.", vars={"topic": "AI"}),
        delivery=Delivery(channel="bus", target="newsout"),
    )
    base.update(kw)
    return AgentRecord(**base)


def _env(cid="wf-1", **data):
    return new_event(stream_id="farm", cid=cid, sid=1, sender="test",
                     event_type="schedule.fired", data=data)


# ============================ GraphRecord model ================================
def test_graph_record_derives_entry_and_validates_edges():
    rec = GraphRecord(
        version="0.1", uid="u1", name="wf",
        nodes=[
            GraphNode(id="a", kind="initiator"),
            GraphNode(id="b", kind="agent", config={"record": {}}),
            GraphNode(id="c", kind="destination", config={"channel": "bus", "target": "x"}),
        ],
        edges=[GraphEdge(src="a", dst="b"), GraphEdge(src="b", dst="c")],
    )
    assert rec.entry == "a"  # derived: the only node with no incoming edge
    assert rec.successors("a") == ["b"]
    assert rec.is_sink("c")


def test_graph_record_rejects_edge_to_missing_node():
    with pytest.raises(Exception) as ei:
        GraphRecord(
            version="0.1", uid="u1", name="wf",
            nodes=[GraphNode(id="a", kind="initiator")],
            edges=[GraphEdge(src="a", dst="ghost")],
        )
    assert "ghost" in str(ei.value)


def test_graph_record_rejects_unknown_major():
    with pytest.raises(Exception) as ei:
        GraphRecord(version="9.0", uid="u1", name="wf",
                    nodes=[GraphNode(id="a", kind="initiator")])
    assert "unsupported DSL major" in str(ei.value)


def test_graph_record_rejects_ambiguous_entry():
    # two roots (no incoming edge) and no explicit entry -> loud.
    with pytest.raises(Exception) as ei:
        GraphRecord(
            version="0.1", uid="u1", name="wf",
            nodes=[GraphNode(id="a", kind="initiator"),
                   GraphNode(id="b", kind="initiator"),
                   GraphNode(id="c", kind="destination", config={"channel": "bus", "target": "x"})],
            edges=[GraphEdge(src="a", dst="c"), GraphEdge(src="b", dst="c")],
        )
    assert "entry" in str(ei.value)


# ============================ Compat shim ======================================
def test_shim_flat_to_degenerate_3_node_graph():
    g = from_flat_record(_flat_record())
    assert isinstance(g, GraphRecord)
    assert g.uid == "00000000-0000-4000-8000-00000000ca11"
    kinds = {n.id: n.kind for n in g.nodes}
    assert kinds == {"initiator": "initiator", "agent": "agent", "bus": "destination"}
    assert g.entry == "initiator"
    pairs = {(e.src, e.dst) for e in g.edges}
    assert pairs == {("initiator", "agent"), ("agent", "bus")}
    # the initiator's asset_ref binds to the flat record uid (the schedule binds here).
    assert g.node("initiator").asset_ref == "00000000-0000-4000-8000-00000000ca11"
    # the agent node carries the whole record; rag/guardrails were stripped (own nodes).
    embed = g.node("agent").config["record"]
    assert embed["brain"]["persona"] == "news_curator"
    assert "rag" not in embed and "guardrails" not in embed


def test_shim_decomposes_rag_and_guardrails_into_own_nodes_in_order():
    flat = _flat_record(
        rag=Rag(domains=["ai"]),
        guardrails=Guardrails(forbidden=["BADWORD"]),
    )
    g = from_flat_record(flat)
    kinds = {n.id: n.kind for n in g.nodes}
    assert kinds == {
        "initiator": "initiator", "rag": "rag", "agent": "agent",
        "guardrail": "guardrail", "bus": "destination",
    }
    # order: initiator -> rag -> agent -> guardrail -> bus
    pairs = {(e.src, e.dst) for e in g.edges}
    assert pairs == {
        ("initiator", "rag"), ("rag", "agent"),
        ("agent", "guardrail"), ("guardrail", "bus"),
    }


# ============================ Registry =========================================
def test_registry_upsert_bumps_version_idempotently():
    reg = GraphRegistry()
    g = from_flat_record(_flat_record())  # version 0.1
    stored = reg.upsert(g)  # first insert keeps its version
    assert stored.version == "0.1"
    assert reg.uids == [g.uid]

    # re-deploy the SAME uid -> updated in place, minor bumped.
    again = reg.upsert(from_flat_record(_flat_record()))
    assert again.version == "0.2"
    assert reg.uids == [g.uid]  # no duplicate
    assert reg.require(g.uid).version == "0.2"

    once_more = reg.upsert(from_flat_record(_flat_record()))
    assert once_more.version == "0.3"


def test_registry_require_unknown_raises():
    reg = GraphRegistry()
    with pytest.raises(KeyError):
        reg.require("nope")


# ============================ Executor: linear =================================
async def test_executor_linear_runs_all_nodes_in_order():
    g = from_flat_record(_flat_record())
    order = []

    async def h(node, value, ctx):
        order.append(node.id)
        return f"{node.id}:{value}"

    handlers = {"initiator": h, "agent": h, "destination": h}
    ctx = await GraphWorkflowExecutor(handlers).run(g, "SEED")
    assert order == ["initiator", "agent", "bus"]
    assert ctx.scratch["run_counts"] == {"initiator": 1, "agent": 1, "bus": 1}


# ============================ Executor: fan-out ================================
async def test_executor_fanout_delivers_to_all_successors():
    # initiator -> agent -> {dest1, dest2}  (fan-out from the agent)
    g = GraphRecord(
        version="0.1", uid="fan", name="fanout",
        nodes=[
            GraphNode(id="init", kind="initiator"),
            GraphNode(id="agent", kind="agent", config={"record": {}}),
            GraphNode(id="d1", kind="destination", config={"channel": "bus", "target": "one"}),
            GraphNode(id="d2", kind="destination", config={"channel": "bus", "target": "two"}),
        ],
        edges=[
            GraphEdge(src="init", dst="agent"),
            GraphEdge(src="agent", dst="d1"),
            GraphEdge(src="agent", dst="d2"),
        ],
    )
    delivered = []

    async def h_pass(node, value, ctx):
        return "ANSWER" if node.kind == "agent" else value

    async def h_dest(node, value, ctx):
        delivered.append((node.id, value))
        return value

    handlers = {"initiator": h_pass, "agent": h_pass, "destination": h_dest}
    ctx = await GraphWorkflowExecutor(handlers).run(g, None)
    # BOTH destinations received the agent's answer.
    assert sorted(delivered) == [("d1", "ANSWER"), ("d2", "ANSWER")]
    assert ctx.scratch["run_counts"]["agent"] == 1


# ============================ Executor: fan-in ================================
async def test_executor_diamond_fanin_no_barrier_runs_twice():
    # init -> {b1, b2} -> agent -> dest.  The agent has TWO incoming edges; with
    # per-message fan-in (no barrier) it runs ONCE PER arrival = twice, and the
    # destination therefore also runs twice. No merge/synchronization.
    g = GraphRecord(
        version="0.1", uid="diamond", name="diamond",
        nodes=[
            GraphNode(id="init", kind="initiator"),
            GraphNode(id="b1", kind="rag", config={"rag": {}}),
            GraphNode(id="b2", kind="rag", config={"rag": {}}),
            GraphNode(id="agent", kind="agent", config={"record": {}}),
            GraphNode(id="dest", kind="destination", config={"channel": "bus", "target": "x"}),
        ],
        edges=[
            GraphEdge(src="init", dst="b1"),
            GraphEdge(src="init", dst="b2"),
            GraphEdge(src="b1", dst="agent"),
            GraphEdge(src="b2", dst="agent"),
            GraphEdge(src="agent", dst="dest"),
        ],
    )
    agent_inputs = []
    dest_inputs = []

    async def h_init(node, value, ctx):
        return "SEED"

    async def h_rag(node, value, ctx):
        return f"{node.id}:{value}"

    async def h_agent(node, value, ctx):
        agent_inputs.append(value)
        return f"ans({value})"

    async def h_dest(node, value, ctx):
        dest_inputs.append(value)
        return value

    handlers = {"initiator": h_init, "rag": h_rag, "agent": h_agent, "destination": h_dest}
    ctx = await GraphWorkflowExecutor(handlers).run(g, None)
    # agent ran TWICE (once per incoming message), once per branch — NO barrier/merge.
    assert ctx.scratch["run_counts"]["agent"] == 2
    assert sorted(agent_inputs) == ["b1:SEED", "b2:SEED"]
    # destination also ran twice (each agent run fanned to it).
    assert ctx.scratch["run_counts"]["dest"] == 2
    assert sorted(dest_inputs) == ["ans(b1:SEED)", "ans(b2:SEED)"]


async def test_executor_vars_edge_is_a_pull_not_a_trigger():
    # init -> agent -> dest, plus data --vars--> agent.  The `vars` edge must NOT enqueue a
    # run of the agent (a PULL input): the agent runs exactly ONCE (from init), and the data
    # node is seeded as an indegree-0 source but does not fan a message into the agent.
    g = GraphRecord(
        version="0.1", uid="pull", name="pull",
        nodes=[
            GraphNode(id="init", kind="initiator"),
            GraphNode(id="data", kind="data", config={"content": {"n": 1}}),
            GraphNode(id="agent", kind="agent", config={"record": {}}),
            GraphNode(id="dest", kind="destination", config={"channel": "bus", "target": "x"}),
        ],
        edges=[
            GraphEdge(src="init", dst="agent"),
            GraphEdge(src="agent", dst="dest"),
            GraphEdge(src="data", dst="agent", dst_port="vars"),
        ],
        entry="init",
    )
    pulled = []

    async def h(node, value, ctx):
        if node.kind == "data":
            return node.config.get("content")
        if node.kind == "agent":
            pulled.append([e.src for e in g.in_edges(node.id, dst_port="vars")])
        return value

    handlers = {k: h for k in ("initiator", "data", "agent", "destination")}
    ctx = await GraphWorkflowExecutor(handlers).run(g, None, extra_seeds=["data"])
    assert ctx.scratch["run_counts"]["agent"] == 1  # NOT triggered by the vars edge
    assert ctx.scratch["run_counts"]["data"] == 1   # ran once (seeded source)
    assert pulled == [["data"]]                      # agent can read its vars source


async def test_executor_seeded_data_source_fans_out_to_a_normal_successor():
    # data --in--> dest, with data an indegree-0 source seeded via extra_seeds. Its output
    # must reach the destination (general runtime flow-source path).
    g = GraphRecord(
        version="0.1", uid="src", name="src",
        nodes=[
            GraphNode(id="init", kind="initiator"),
            GraphNode(id="data", kind="data", config={"content": {"k": "v"}}),
            GraphNode(id="dest", kind="destination", config={"channel": "bus", "target": "x"}),
        ],
        edges=[GraphEdge(src="data", dst="dest")],
        entry="init",
    )
    delivered = []

    async def h(node, value, ctx):
        if node.kind == "data":
            return node.config.get("content")
        if node.kind == "destination":
            delivered.append(value)
        return value

    handlers = {k: h for k in ("initiator", "data", "destination")}
    await GraphWorkflowExecutor(handlers).run(g, None, extra_seeds=["data"])
    assert delivered == [{"k": "v"}]


async def test_executor_unknown_kind_is_loud():
    g = GraphRecord(
        version="0.1", uid="u", name="w",
        nodes=[GraphNode(id="a", kind="initiator"),
               GraphNode(id="b", kind="destination", config={"channel": "bus", "target": "x"})],
        edges=[GraphEdge(src="a", dst="b")],
    )

    async def h(node, value, ctx):
        return value

    with pytest.raises(GraphExecutionError):
        # no handler for 'destination'
        await GraphWorkflowExecutor({"initiator": h}).run(g, None)


# ============================ Runner e2e via shim ==============================
async def test_runner_graph_record_linear_delivers_like_today():
    bus = FakeBus()
    agent_server = FakeAgentServer([{"role": "assistant", "content": "curated headlines"}])
    runner = Runner(_settings(), bus, agent_server=agent_server)

    g = from_flat_record(_flat_record())
    await runner.run_graph_record(g, _env())

    # the agent ran with the built task (input template applied).
    assert agent_server.calls, "agent_server.chat was never called"
    assert "Curate AI." in agent_server.calls[0]["messages"][-1]["content"]

    types = [e.header.event_type for _s, e in bus.published]
    assert "agent.result" in types
    assert "workflow.terminated" in types

    # delivered the final answer to the bus destination stream.
    delivered = [(s, e) for s, e in bus.published if s == "stream:newsout"]
    assert delivered, f"no delivery to stream:newsout; got {[s for s,_ in bus.published]}"
    assert delivered[-1][1].payload.data.get("output") == "curated headlines"


async def test_runner_run_flat_via_shim_matches_graph_path():
    bus = FakeBus()
    agent_server = FakeAgentServer([{"role": "assistant", "content": "curated headlines"}])
    runner = Runner(_settings(), bus, agent_server=agent_server)

    # the compat entry: a legacy flat record runs via the graph executor unchanged.
    await runner.run_flat_via_shim(_flat_record(), _env(cid="shim-1"))

    delivered = [(s, e) for s, e in bus.published if s == "stream:newsout"]
    assert delivered and delivered[-1][1].payload.data.get("output") == "curated headlines"


async def test_runner_graph_record_runs_decomposed_rag_and_guardrail_in_order():
    bus = FakeBus()
    agent_server = FakeAgentServer([{"role": "assistant", "content": "clean output"}])
    runner = Runner(_settings(), bus, agent_server=agent_server)

    flat = _flat_record(
        rag=Rag(domains=["ai"]),
        guardrails=Guardrails(forbidden=["FORBIDDEN"]),
    )
    g = from_flat_record(flat)
    await runner.run_graph_record(g, _env(cid="decomp-1"))

    types = [e.header.event_type for _s, e in bus.published]
    # the RAG node ran (its own stage) BEFORE the agent produced its result.
    assert "rag.retrieved" in types
    assert types.index("rag.retrieved") < types.index("agent.result")
    # guardrail did NOT block (output is clean), so delivery happened.
    delivered = [(s, e) for s, e in bus.published if s == "stream:newsout"]
    assert delivered, "clean output should have been delivered"


async def test_runner_graph_record_guardrail_node_blocks_loudly():
    bus = FakeBus()
    agent_server = FakeAgentServer([{"role": "assistant", "content": "contains FORBIDDEN token"}])
    runner = Runner(_settings(), bus, agent_server=agent_server)

    flat = _flat_record(guardrails=Guardrails(forbidden=["FORBIDDEN"]))
    g = from_flat_record(flat)
    with pytest.raises(RuntimeError, match="guardrail"):
        await runner.run_graph_record(g, _env(cid="block-1"))

    # nothing delivered; a terminal guardrail_blocked event was emitted.
    delivered = [(s, e) for s, e in bus.published if s == "stream:newsout"]
    assert not delivered, "blocked output must not be delivered"
    reasons = [e.payload.data.get("reason") for _s, e in bus.published
               if e.header.event_type == "workflow.terminated"]
    assert "guardrail_blocked" in reasons
