"""The brain node — the server-side function-calling loop.

The proven pattern from noted's ``dispatch_tool_calling``: advertise the MCP tools
(as OpenAI specs) to an agent_server preset, run a bounded loop — each round POSTs
to agent_server; if the model returns ``tool_calls`` we execute them via MCP, append
``role:'tool'`` results, and continue; otherwise we take the final content. The loop
is **framework-bounded** (``max_rounds``) — we never trust the model to stop.

A tool failure is fed back to the model as the tool result (so it can recover) AND
logged loudly — surfaced, never silently swallowed. agent_server emits ``<think>``
inside ``content``; we split it out so it can be observed but not delivered.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..agent_server_client import AgentServerClient
from ..dsl import AgentRecord
from ..mcp_client import MCPClient, MCPError
from ..skills.registry import SkillRegistry

log = logging.getLogger("agent_runtime.brain")

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)

# The name of the on-demand skill-fetch tool exposed in the loop (block_management.md §8.3).
GET_SKILL_TOOL = "get_skill"

# The OpenAI tool spec for get_skill — advertised alongside the MCP tools so the model can
# pull a non-auto-injected skill's body on demand. Kept static: one string arg ``name``.
GET_SKILL_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": GET_SKILL_TOOL,
        "description": (
            "Fetch the full instructions (body) of a named skill from the skill "
            "registry. Use this when a skill listed in the system prompt is relevant "
            "but its body was not already injected. Argument: name (the skill name)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill name exactly as listed in the registry.",
                }
            },
            "required": ["name"],
        },
    },
}


def build_skill_injection(
    record: AgentRecord, skills: SkillRegistry | None
) -> tuple[str, list[str], list[dict], set[str]]:
    """Assemble the skill context for one brain run (block_management.md §8.3).

    Reads the Agent's ``skills`` allow-list + context conditions off ``record.skills``
    and, using the registry:
      1. builds the **registry advertisement** (name + description) for the allow-list,
      2. **auto-injects** the bodies of priority-1 skills whose triggers match the
         context (within the registry's hard token budget — RAISES if exceeded),
      3. yields the **get_skill tool spec** so the loop can fetch the rest on demand.

    Returns ``(preamble_text, injected_names, tool_specs, injected_name_set)``. The
    ``injected_name_set`` is what get_skill must refuse to re-fetch (must not re-serve an
    already auto-injected skill's body). Degrades to empty when there is no registry or
    no selected skills — never crashes the run."""
    if skills is None or record.skills is None or not record.skills.allow:
        return "", [], [], set()

    allow = record.skills.allow
    context = record.skills.context

    registry_text = skills.get_registry_text(names=allow)
    # Auto-inject priority-1 skills whose triggers match the run's context (may RAISE on
    # budget overflow — surfaced loudly, never silently truncated).
    static = skills.get_static_skills(context, names=allow)

    parts: list[str] = []
    if registry_text:
        parts.append(registry_text)
    injected_names: list[str] = []
    for name, body in static:
        injected_names.append(name)
        parts.append(f"\n## Skill: {name}\n{body}")
    preamble = "\n".join(parts).strip()
    return preamble, injected_names, [GET_SKILL_TOOL_SPEC], set(injected_names)


def _handle_get_skill(
    args: dict,
    skills: SkillRegistry | None,
    injected_set: set[str],
    record: AgentRecord,
) -> str:
    """Execute one ``get_skill`` tool call. Returns the body text fed back to the model,
    or a plain-text ERROR string it can recover from (never raises)."""
    name = str((args or {}).get("name") or "").strip()
    if not name:
        return "ERROR: get_skill requires a 'name' argument"
    if skills is None:
        return "ERROR: skill registry is not available"
    # Enforce the Agent's allow-list — get_skill cannot reach outside the selected skills.
    allow = set(record.skills.allow) if record.skills else set()
    if name not in allow:
        return f"ERROR: skill '{name}' is not in this agent's selected skills"
    if name in injected_set:
        # Already auto-injected — do NOT re-serve the body (§8.3).
        return (
            f"Skill '{name}' is already active in your context above; no need to "
            f"fetch it again."
        )
    body = skills.get_skill(name)
    if body is None:
        return f"ERROR: no skill named '{name}' in the registry"
    return body

# Optional callback for observability: (turn, tool_name, args, result) -> None.
ToolObserver = Callable[[int, str, dict, str], Awaitable[None]]


@dataclass
class BrainResult:
    answer: str                         # final content, <think> stripped
    thought: str = ""                   # concatenated <think> blocks (observable)
    turns_used: int = 0
    hit_cap: bool = False               # loop ended on max_rounds without a final answer
    tool_log: list[dict] = field(default_factory=list)


def split_think(content: str) -> tuple[str, str]:
    """Return (thought, answer): pull out <think>…</think>, leave the rest."""
    thoughts = "\n".join(m.strip() for m in _THINK_RE.findall(content))
    answer = _THINK_RE.sub("", content).strip()
    return thoughts, answer


async def run_brain(
    record: AgentRecord,
    task_text: str,
    *,
    agent_server: AgentServerClient,
    mcp: MCPClient | None = None,
    on_tool: ToolObserver | None = None,
    skills: SkillRegistry | None = None,
) -> BrainResult:
    persona = record.brain.persona
    overrides = record.brain.llm.as_overrides()

    tools_spec: list[dict] | None = None
    max_rounds = 1
    if record.tools and record.tools.allow:
        if mcp is None:
            raise RuntimeError(
                f"agent '{record.name}' declares tools but no MCP client was provided"
            )
        tools_spec = await mcp.openai_tools(record.tools.allow)
        max_rounds = record.tools.max_rounds

    # Skills (block_management.md §8.3): advertise the selected skills + auto-inject the
    # priority-1 ones whose triggers match, and expose get_skill for on-demand fetch.
    skill_preamble, _injected, skill_tool_specs, injected_set = build_skill_injection(
        record, skills
    )
    messages: list[dict] = []
    if skill_preamble:
        messages.append({"role": "system", "content": skill_preamble})
    messages.append({"role": "user", "content": task_text})

    # get_skill lives in the loop whenever the agent has selected skills — even a tool-less
    # agent can pull a skill body on demand, so give the loop at least a couple of rounds.
    if skill_tool_specs and record.skills and record.skills.allow:
        tools_spec = (tools_spec or []) + skill_tool_specs
        if max_rounds < 2:
            max_rounds = 2

    tool_log: list[dict] = []
    final = ""
    hit_cap = True
    turns = 0

    for turn in range(1, max_rounds + 1):
        turns = turn
        msg = await agent_server.chat(
            persona, messages, tools=tools_spec, overrides=overrides
        )
        calls = msg.get("tool_calls") or []
        if not calls:
            final = msg.get("content") or ""
            hit_cap = False
            break

        # echo the assistant turn (with its tool_calls) back into the history
        messages.append(
            {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
        )
        for tc in calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError as exc:
                log.error("tool '%s' has unparseable args %r: %s", name, raw_args, exc)
                result = f"ERROR: could not parse tool arguments: {exc}"
                args = {}
            else:
                if name == GET_SKILL_TOOL:
                    # On-demand skill fetch (§8.3). Must NOT re-serve an already
                    # auto-injected skill (it's already in the system prompt).
                    result = _handle_get_skill(args, skills, injected_set, record)
                elif mcp is None:
                    # An agent with skills-only (no MCP) can still be asked a non-skill
                    # tool by a confused model — surface it, don't crash.
                    log.error("tool '%s' called but no MCP client is available", name)
                    result = f"ERROR: tool '{name}' is not available (no MCP client)"
                else:
                    try:
                        result = await mcp.call(name, args)
                    except MCPError as exc:
                        # Surfaced (logged) + fed back so the model can recover — not swallowed.
                        log.error("tool '%s' failed: %s", name, exc)
                        result = f"ERROR calling {name}: {exc}"
            tool_log.append({"turn": turn, "name": name, "args": args})
            if on_tool is not None:
                await on_tool(turn, name, args, result)
            messages.append(
                {"role": "tool", "tool_call_id": tc.get("id", ""), "content": result}
            )

    thought, answer = split_think(final)
    if not answer.strip():
        # No final answer — either the loop hit the round cap, or the model reasoned
        # only inside <think> and emitted nothing after it. Force one tool-less round
        # demanding the answer (and no <think>) so we never deliver empty.
        log.warning(
            "agent '%s' produced no final answer (hit_cap=%s); forcing a tool-less "
            "final round", record.name, hit_cap,
        )
        msg = await agent_server.chat(
            persona,
            messages + [{"role": "user", "content":
                         "Stop calling tools and do NOT use a <think> block. Output "
                         "ONLY the final answer now, using the tool results above."}],
            tools=None,
            overrides=overrides,
        )
        forced = msg.get("content") or ""
        t2, a2 = split_think(forced)
        if t2:
            thought = f"{thought}\n{t2}".strip() if thought else t2
        # Prefer the clean answer; fall back to the raw forced text, then to the
        # reasoning we captured — anything but empty.
        answer = a2.strip() or forced.strip() or answer
    return BrainResult(
        answer=answer, thought=thought, turns_used=turns, hit_cap=hit_cap, tool_log=tool_log
    )
