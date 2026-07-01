"""The runner executes the agent as a composer GRAPH (trigger → agent → destination),
via the GraphExecutor — verified offline with fakes.

Proves the runtime migration: the same pipeline behaviour (task → brain → guardrail →
delivery, with run events) now flows through the new graph structure, and the IR built
from a record has the expected trigger→agent→destination shape.
"""

import dataclasses

from agent_bus_client import new_event

from agent_runtime.config import Settings
from agent_runtime.dsl import AgentRecord, Brain, Delivery, Input
from agent_runtime.runner import Runner, ir_from_record


class FakeAgentServer:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []

    async def chat(self, model, messages, *, tools=None, overrides=None):
        self.calls.append({"model": model, "messages": list(messages)})
        return self._scripted.pop(0)


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


def _record():
    return AgentRecord(
        version="0.1",
        uid="00000000-0000-4000-8000-00000000ca11",
        name="news-morning-ai",
        brain=Brain(persona="news_curator"),
        input=Input(template="Curate {topic}.", vars={"topic": "AI"}),
        delivery=Delivery(channel="bus", target="newsout"),
    )


def test_ir_from_record_is_trigger_agent_destination():
    ir = ir_from_record(_record())
    assert ir.entry == "trigger"
    assert set(ir.nodes) == {"trigger", "agent", "bus"}
    assert ir.nodes["bus"].kind == "bus"
    # edges: trigger -> agent -> bus
    pairs = {(e.src, e.dst) for e in ir.edges}
    assert pairs == {("trigger", "agent"), ("agent", "bus")}


async def test_runner_executes_via_graph_and_delivers():
    bus = FakeBus()
    agent_server = FakeAgentServer([{"role": "assistant", "content": "curated headlines"}])
    settings = dataclasses.replace(Settings(), runs_stream_id="runs", sender_id="agent-runtime")
    runner = Runner(settings, bus, agent_server=agent_server)

    env = new_event(stream_id="farm", cid="wf-1", sid=1, sender="test",
                    event_type="schedule.fired", data={})
    await runner.run(_record(), env)

    # the agent ran (brain called with the built task)
    assert agent_server.calls, "agent_server.chat was never called"
    assert "Curate AI." in agent_server.calls[0]["messages"][-1]["content"]

    # run events emitted through the graph walk
    types = [e.header.event_type for _s, e in bus.published]
    assert "agent.result" in types
    assert "workflow.terminated" in types

    # the destination handler delivered to the bus stream
    delivered = [(s, e) for s, e in bus.published if s == "stream:newsout"]
    assert delivered, f"no delivery to stream:newsout; published to {[s for s,_ in bus.published]}"
    out_env = delivered[-1][1]
    assert out_env.payload.data.get("output") == "curated headlines"
