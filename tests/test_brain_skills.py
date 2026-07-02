"""Brain-node skill injection tests (block_management.md §8.3): the registry
advertisement + auto-injected priority-1 bodies reach the system prompt, the
get_skill tool spec is advertised in the loop, and get_skill fetches on demand
(refusing to re-serve an already auto-injected skill and honoring the allow-list)."""

from __future__ import annotations

from pathlib import Path

from agent_runtime.dsl import AgentRecord, Brain, Delivery, Skills, Tools
from agent_runtime.nodes.brain import (
    GET_SKILL_TOOL,
    build_skill_injection,
    run_brain,
)
from agent_runtime.skills.registry import SkillRegistry

FIXTURES = Path(__file__).parent / "skill_fixtures"


def _registry() -> SkillRegistry:
    return SkillRegistry(str(FIXTURES))


class FakeAgentServer:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []

    async def chat(self, model, messages, *, tools=None, overrides=None):
        self.calls.append({"model": model, "messages": list(messages), "tools": tools})
        return self._scripted.pop(0)


def _record(*, allow, context, with_tools=False):
    return AgentRecord(
        version="0.1",
        uid="00000000-0000-4000-8000-0000000000c1",
        name="skilled",
        brain=Brain(persona="general"),
        skills=Skills(allow=allow, context=context),
        tools=Tools(server="mcp", allow=["mcp__web_search"]) if with_tools else None,
        delivery=Delivery(channel="bus", target="x"),
    )


# --- build_skill_injection (pure) -------------------------------------------

def test_injection_advertises_and_auto_injects():
    rec = _record(allow=["news-curation", "deep-analysis"], context=["news"])
    preamble, injected, tool_specs, injected_set = build_skill_injection(rec, _registry())

    # advertisement lists both selected skills (name + description)
    assert "news-curation" in preamble
    assert "deep-analysis" in preamble
    # priority-1 news-curation auto-injected (trigger 'news'); its BODY is present
    assert "Group related headlines" in preamble
    assert injected == ["news-curation"]
    assert injected_set == {"news-curation"}
    # priority-3 deep-analysis is advertised but NOT auto-injected (body absent)
    assert "claim, evidence, counter-evidence" not in preamble
    # the get_skill tool is offered
    assert tool_specs and tool_specs[0]["function"]["name"] == GET_SKILL_TOOL


def test_injection_noop_without_selected_skills():
    rec = AgentRecord(
        version="0.1",
        uid="00000000-0000-4000-8000-0000000000c2",
        name="bare",
        brain=Brain(persona="general"),
        delivery=Delivery(channel="bus", target="x"),
    )
    preamble, injected, tool_specs, injected_set = build_skill_injection(rec, _registry())
    assert preamble == "" and injected == [] and tool_specs == [] and injected_set == set()


# --- run_brain wiring -------------------------------------------------------

async def test_run_brain_prepends_skill_system_prompt_and_advertises_get_skill():
    rec = _record(allow=["news-curation"], context=["news"])
    fas = FakeAgentServer([{"role": "assistant", "content": "Digest ready."}])
    res = await run_brain(rec, "curate", agent_server=fas, mcp=None, skills=_registry())

    assert res.answer == "Digest ready."
    first = fas.calls[0]
    # a system message carries the skill preamble
    sys_msgs = [m for m in first["messages"] if m["role"] == "system"]
    assert sys_msgs and "Group related headlines" in sys_msgs[0]["content"]
    # get_skill is advertised as a tool even for a tool-less agent
    tool_names = [t["function"]["name"] for t in (first["tools"] or [])]
    assert GET_SKILL_TOOL in tool_names


async def test_run_brain_get_skill_fetches_on_demand():
    # deep-analysis is priority-3 (never auto-injected); the model fetches it via get_skill.
    rec = _record(allow=["deep-analysis"], context=[])
    fas = FakeAgentServer(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "s1",
                        "function": {
                            "name": GET_SKILL_TOOL,
                            "arguments": '{"name": "deep-analysis"}',
                        },
                    }
                ],
            },
            {"role": "assistant", "content": "Analysed."},
        ]
    )
    res = await run_brain(rec, "analyse", agent_server=fas, mcp=None, skills=_registry())
    assert res.answer == "Analysed."
    # the fetched body was fed back as the tool message
    tool_msg = fas.calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert "claim, evidence, counter-evidence" in tool_msg["content"]


async def test_get_skill_refuses_already_injected():
    rec = _record(allow=["news-curation"], context=["news"])  # auto-injected
    fas = FakeAgentServer(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "s1",
                        "function": {
                            "name": GET_SKILL_TOOL,
                            "arguments": '{"name": "news-curation"}',
                        },
                    }
                ],
            },
            {"role": "assistant", "content": "ok"},
        ]
    )
    res = await run_brain(rec, "go", agent_server=fas, mcp=None, skills=_registry())
    assert res.answer == "ok"
    tool_msg = fas.calls[1]["messages"][-1]
    assert "already active" in tool_msg["content"]


async def test_get_skill_refuses_outside_allow_list():
    rec = _record(allow=["deep-analysis"], context=[])
    fas = FakeAgentServer(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "s1",
                        "function": {
                            "name": GET_SKILL_TOOL,
                            "arguments": '{"name": "market-rules"}',  # not selected
                        },
                    }
                ],
            },
            {"role": "assistant", "content": "done"},
        ]
    )
    res = await run_brain(rec, "go", agent_server=fas, mcp=None, skills=_registry())
    tool_msg = fas.calls[1]["messages"][-1]
    assert "not in this agent's selected skills" in tool_msg["content"]
