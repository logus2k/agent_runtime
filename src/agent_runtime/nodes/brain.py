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

log = logging.getLogger("agent_runtime.brain")

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)

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
) -> BrainResult:
    persona = record.brain.persona
    overrides = record.brain.llm.as_overrides()

    tools_spec = None
    max_rounds = 1
    if record.tools and record.tools.allow:
        if mcp is None:
            raise RuntimeError(
                f"agent '{record.id}' declares tools but no MCP client was provided"
            )
        tools_spec = await mcp.openai_tools(record.tools.allow)
        max_rounds = record.tools.max_rounds

    messages: list[dict] = [{"role": "user", "content": task_text}]
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
                try:
                    result = await mcp.call(name, args)  # type: ignore[union-attr]
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
            "final round", record.id, hit_cap,
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
