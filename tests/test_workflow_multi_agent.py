"""Multi-agent workflow execution — the first vertical slice (agent → agent chain).

Proves the GAP the design called out (`documents/designs/multi_agent_execution.md`): a
2-agent workflow that CANNOT lower to a single flat `AgentRecord` now lowers to a
graph-form `IRGraph` via `Graph.to_workflow_ir()` and executes through the (unchanged)
`GraphExecutor`, with data flowing edge-to-edge — the planner's answer becomes the
writer's task. All fakes; no live services. Mirrors `test_runner_graph.py`.
"""

import copy
import dataclasses
import json
from pathlib import Path

from agent_bus_client import new_event

from agent_runtime.composer import ExecContext, GraphExecutor, IRGraph
from agent_runtime.composer.lower import Graph
from agent_runtime.config import Settings
from agent_runtime.runner import Runner

FIXTURES = Path(__file__).parent / "fixtures"


def _planner_writer_graph() -> dict:
    return json.loads((FIXTURES / "planner_writer.graph.json").read_text())


# --- Fakes (mirrors test_runner_graph.py) --------------------------------------
class FakeAgentServer:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []

    async def chat(self, model, messages, *, tools=None, overrides=None):
        self.calls.append({"model": model, "messages": list(messages), "tools": tools})
        return self._scripted.pop(0)


class FakeSio:
    """Minimal WhatsApp bridge socket (mirrors test_delivery.FakeSio)."""

    def __init__(self, ack):
        self._ack = ack
        self.sent = None
        self.disconnected = False

    async def connect(self, url, namespaces=None, auth=None):
        self.url = url

    async def call(self, event, data, namespace=None, timeout=None):
        self.sent = {"event": event, "data": data}
        return self._ack

    async def disconnect(self):
        self.disconnected = True


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


# --- Lowering: to_workflow_ir() -------------------------------------------------
def test_to_workflow_ir_shape():
    ir = Graph(_planner_writer_graph()).to_workflow_ir()
    assert isinstance(ir, IRGraph)
    # 4 nodes: trigger, two agents, one destination.
    assert set(ir.nodes) == {"trigger:1", "agent:2", "agent:3", "whatsapp:4"}
    # entry is the trigger.
    assert ir.entry == "trigger:1"
    # edges: trigger -> a1 -> a2 -> dest, all on the plain "out" port.
    pairs = {(e.src, e.dst) for e in ir.edges}
    assert pairs == {
        ("trigger:1", "agent:2"),
        ("agent:2", "agent:3"),
        ("agent:3", "whatsapp:4"),
    }
    assert all(e.port == "out" for e in ir.edges)


def test_to_workflow_ir_agent_nodes_carry_own_config():
    ir = Graph(_planner_writer_graph()).to_workflow_ir()
    a1 = ir.nodes["agent:2"].config["record"]
    a2 = ir.nodes["agent:3"].config["record"]
    # each agent node carries its own persona + input fragment.
    assert a1["brain"]["persona"] == "planner"
    assert a2["brain"]["persona"] == "writer"
    assert a1["input"]["template"] == "Draft a plan about {topic}."
    assert a1["input"]["vars"] == {"topic": "AI agents"}
    assert a2["input"]["template"] == ""  # writer consumes the incoming value verbatim
    # destination node carries the target.
    assert ir.nodes["whatsapp:4"].config["target"] == "120363427427912302@g.us"


def test_linear_lower_untouched_multi_agent_still_fails_linear_path():
    """Back-compat guard: the linear lower() still rejects >1 agent (unchanged)."""
    res = Graph(_planner_writer_graph()).lower()
    assert res["ok"] is False
    assert any("Agent" in e for e in res["errors"])


# --- Executor routing (fakes) --------------------------------------------------
async def test_executor_routes_chain_and_passes_data_edge_to_edge():
    ir = Graph(_planner_writer_graph()).to_workflow_ir()
    order: list[str] = []
    received: dict[str, object] = {}

    async def h_trigger(node, value, ctx):
        order.append(node.id)
        return "SEED"

    async def h_agent(node, value, ctx):
        order.append(node.id)
        received[node.id] = value
        return "PLAN" if node.id == "agent:2" else f"WROTE({value})"

    async def h_dest(node, value, ctx):
        order.append(node.id)
        received[node.id] = value
        return value

    handlers = {"trigger": h_trigger, "agent": h_agent, "whatsapp": h_dest}
    out = await GraphExecutor(handlers).run(ir, None, ExecContext())

    assert order == ["trigger:1", "agent:2", "agent:3", "whatsapp:4"]
    # agent2 received agent1's answer ("PLAN") as its incoming value — edge-to-edge.
    assert received["agent:3"] == "PLAN"
    assert out == "WROTE(PLAN)"


# --- Runner e2e (FakeAgentServer scripted, FakeBus) ----------------------------
async def test_run_workflow_e2e_chains_two_agents_and_delivers():
    bus = FakeBus()
    # two scripted responses: planner then writer.
    agent_server = FakeAgentServer([
        {"role": "assistant", "content": "PLAN: three sections"},
        {"role": "assistant", "content": "FINAL ARTICLE"},
    ])
    settings = dataclasses.replace(
        Settings(), runs_stream_id="runs", sender_id="agent-runtime",
        whatsapp_token="secret-token", whatsapp_agent_name="wf-agent",
        whatsapp_bridge_url="http://whatsapp-bridge:3399",
    )
    sio = FakeSio({"ok": True, "messageId": "mid-final"})
    runner = Runner(settings, bus, agent_server=agent_server, sio_factory=lambda: sio)

    ir = Graph(_planner_writer_graph()).to_workflow_ir()
    env = new_event(stream_id="farm", cid="wf-multi", sid=1, sender="test",
                    event_type="schedule.fired", data={})
    await runner.run_workflow(ir, env)

    # exactly two agent_server calls (one per agent).
    assert len(agent_server.calls) == 2, agent_server.calls

    # call 1 = planner: its own template applied.
    planner_task = agent_server.calls[0]["messages"][-1]["content"]
    assert "Draft a plan about AI agents." == planner_task

    # call 2 = writer: its task CONTAINS call 1's answer (edge-to-edge proven).
    writer_task = agent_server.calls[1]["messages"][-1]["content"]
    assert "PLAN: three sections" in writer_task

    # tool-less: no tools advertised to either agent.
    assert agent_server.calls[0]["tools"] is None
    assert agent_server.calls[1]["tools"] is None

    # delivery carries the FINAL answer to the whatsapp bridge (edge-to-edge sink).
    assert sio.sent is not None, "whatsapp bridge never received a sendMessage"
    assert sio.sent["event"] == "sendMessage"
    assert sio.sent["data"] == {
        "targetId": "120363427427912302@g.us", "text": "FINAL ARTICLE",
    }

    types = [e.header.event_type for _s, e in bus.published]
    assert "agent.result" in types
    assert "workflow.terminated" in types
    # edge.traversed emitted for each edge crossed (optional but wired).
    assert types.count("edge.traversed") == 3

    # the delivery agent.result output is the writer's final answer.
    delivery_results = [
        e.payload.data for _s, e in bus.published
        if e.header.event_type == "agent.result"
        and e.payload.data.get("channel") == "whatsapp"
    ]
    assert delivery_results and delivery_results[-1]["output"] == "FINAL ARTICLE"
