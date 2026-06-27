"""Step 3: the brain node — the function-calling loop.

Deterministic unit tests use fakes (no services). A live integration test exercises
the real agent_server preset + mcp-service web_search; it SKIPS loudly if either is
unreachable. (newsapi_search/fetch_url aren't on mcp-service yet — web_search proves
the loop end-to-end in the meantime.)
"""

import os

import pytest

from agent_runtime.agent_server_client import AgentServerClient
from agent_runtime.dsl import AgentRecord, Brain, Delivery, Tools
from agent_runtime.mcp_client import MCPClient
from agent_runtime.nodes.brain import run_brain, split_think


# --- fakes ------------------------------------------------------------------

class FakeAgentServer:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []

    async def chat(self, model, messages, *, tools=None, overrides=None):
        self.calls.append({"model": model, "messages": list(messages), "tools": tools})
        return self._scripted.pop(0)


class FakeMCP:
    def __init__(self):
        self.called = []

    async def openai_tools(self, allow):
        return [{"type": "function", "function": {"name": n}} for n in allow]

    async def call(self, name, args):
        self.called.append((name, args))
        return f"RESULT::{name}"


def _record(with_tools=True, max_rounds=3):
    return AgentRecord(
        version="0.1",
        id="t",
        brain=Brain(persona="general"),
        tools=Tools(server="mcp", allow=["mcp__web_search"], max_rounds=max_rounds)
        if with_tools
        else None,
        delivery=Delivery(channel="bus", target="x"),
    )


# --- unit: think splitting --------------------------------------------------

def test_split_think():
    thought, answer = split_think("<think>reasoning here</think>The answer.")
    assert thought == "reasoning here"
    assert answer == "The answer."


# --- unit: the loop ---------------------------------------------------------

async def test_brain_executes_tool_then_finalizes():
    fas = FakeAgentServer(
        [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "function": {"name": "mcp__web_search",
                                                      "arguments": '{"query": "AI"}'}}]},
            {"role": "assistant", "content": "<think>done</think>Here are the headlines."},
        ]
    )
    fmcp = FakeMCP()
    res = await run_brain(_record(), "curate headlines", agent_server=fas, mcp=fmcp)

    assert res.answer == "Here are the headlines."
    assert res.thought == "done"
    assert res.turns_used == 2
    assert res.hit_cap is False
    assert fmcp.called == [("mcp__web_search", {"query": "AI"})]
    # tools were advertised to the model
    assert fas.calls[0]["tools"] == [{"type": "function", "function": {"name": "mcp__web_search"}}]


async def test_brain_no_tools_single_round():
    fas = FakeAgentServer([{"role": "assistant", "content": "Just an answer."}])
    res = await run_brain(_record(with_tools=False), "hi", agent_server=fas, mcp=None)
    assert res.answer == "Just an answer."
    assert res.turns_used == 1


async def test_brain_tool_error_is_fed_back_not_raised():
    from agent_runtime.mcp_client import MCPError

    class ErrMCP(FakeMCP):
        async def call(self, name, args):
            raise MCPError("boom")

    fas = FakeAgentServer(
        [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "function": {"name": "mcp__web_search",
                                                      "arguments": "{}"}}]},
            {"role": "assistant", "content": "Recovered."},
        ]
    )
    res = await run_brain(_record(), "go", agent_server=fas, mcp=ErrMCP())
    # the loop recovered; the tool error was fed back as the tool message
    assert res.answer == "Recovered."
    tool_msg = fas.calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert "ERROR calling mcp__web_search" in tool_msg["content"]


async def test_brain_hits_cap_when_model_never_stops():
    # model always asks for a tool -> loop ends at max_rounds with hit_cap
    forever = [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": f"c{i}", "function": {"name": "mcp__web_search",
                                                     "arguments": "{}"}}]}
        for i in range(5)
    ]
    fas = FakeAgentServer(forever)
    res = await run_brain(_record(max_rounds=2), "go", agent_server=fas, mcp=FakeMCP())
    assert res.hit_cap is True
    assert res.turns_used == 2


# --- live integration -------------------------------------------------------

AGENT_SERVER = os.getenv("AGENT_SERVER_TEST_URL", "http://127.0.0.1:7701")
MCP = os.getenv("MCP_TEST_URL", "http://127.0.0.1:4950/mcp/")
LIVE_PRESET = os.getenv("BRAIN_TEST_PRESET", "general")


async def test_brain_live_web_search():
    import httpx

    # Probe both services; skip loudly if either is down.
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(MCP, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                    "params": {}},
                         headers={"Accept": "application/json, text/event-stream"})
            await c.get(f"{AGENT_SERVER}/health")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live services not reachable (agent_server/mcp-service): {exc}")

    record = AgentRecord(
        version="0.1",
        id="live",
        brain=Brain(persona=LIVE_PRESET),
        tools=Tools(server="mcp", allow=["mcp__web_search"], max_rounds=4),
        delivery=Delivery(channel="bus", target="x"),
    )
    agent_server = AgentServerClient(AGENT_SERVER, timeout_s=120)
    mcp = MCPClient(MCP, server="mcp", timeout_s=40)

    res = await run_brain(
        record,
        "Use the mcp__web_search tool to search for 'AI agents', then give me the "
        "top 3 result titles as a short list. Call the tool before answering.",
        agent_server=agent_server,
        mcp=mcp,
    )
    # the brain actually invoked the real tool and produced a non-empty answer
    assert any(t["name"] == "mcp__web_search" for t in res.tool_log), (
        f"model did not call web_search; tool_log={res.tool_log}"
    )
    assert res.answer.strip(), "empty answer from live brain"
