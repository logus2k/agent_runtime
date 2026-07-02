"""The outer loop — repeats a whole agent invocation (block_management.md §8.4).

This is the **outer** loop around the Agent node's invocation, distinct from the Brain
node's **inner** ``tools.max_rounds`` tool-calling loop (which runs within a *single*
invocation). The four types are **separate, not composable**:

  * ``off``        — run the agent exactly once.
  * ``counter``    — run it exactly ``n`` times; the last outcome is the result.
  * ``expression`` — repeat until the configured expression matches the outcome; a HARD
                     ``max_iter`` cap force-exits (never trust the model to stop).
  * ``judge``      — each iteration run the embedded Judge over the outcome and read its
                     verdict (expression-on-output OR structured field); stop when it
                     validates; a HARD ``max_iter`` cap force-exits.

``iteration_input`` chooses what feeds each iteration after the first: ``same`` re-uses
the original task (pure retry) or ``previous`` reinserts the prior outcome (refinement).

The loop is driven by two injected async callables so it stays free of runner/agent_server
wiring and is unit-testable in isolation:

  * ``run_agent(task) -> outcome`` — one whole agent invocation.
  * ``run_judge(judge, task) -> verdict_text`` — one Judge invocation (only for ``judge``).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from ..dsl import Judge, Loop, Verdict

log = logging.getLogger("agent_runtime.loop")

# One agent invocation: task text -> outcome text.
RunAgent = Callable[[str], Awaitable[str]]
# One Judge invocation: (judge, task text) -> the Judge's raw output text.
RunJudge = Callable[[Judge, str], Awaitable[str]]

_REGEX_RE = re.compile(r"^/(.*)/$", re.DOTALL)


def expression_matches(expression: str, text: str) -> bool:
    """Does ``expression`` match ``text``? Substring by default; if the expression is
    wrapped in slashes (``/pattern/``) it is treated as a regex (``re.search``). An empty
    expression never matches (a loud caller-side error already guards the config)."""
    if not expression:
        return False
    m = _REGEX_RE.match(expression)
    if m is not None:
        return re.search(m.group(1), text) is not None
    return expression in text


def _read_field(field_path: str, text: str) -> bool:
    """Read a dotted-path boolean out of a JSON object in ``text`` (verdict read=field).

    The Judge's output is parsed as JSON; the dotted path (e.g. ``result.passed``) is
    walked and the value's truthiness is the verdict. A non-JSON output or a missing path
    is a *non-validation* (continue looping) — logged loudly, never a silent crash."""
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        log.warning("verdict field-read: Judge output is not JSON (%s): %r", exc, text[:200])
        return False
    cur: object = obj
    for part in field_path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            log.warning("verdict field-read: path '%s' not found in Judge output", field_path)
            return False
    return bool(cur)


def verdict_validates(verdict: Verdict, judge_output: str) -> bool:
    """Turn the Judge's raw text into a stop/continue decision per ``verdict.read`` (§8.4)."""
    if verdict.read == "expression":
        return expression_matches(verdict.expression, judge_output)
    return _read_field(verdict.field, judge_output)


@dataclass
class LoopResult:
    outcome: str                       # the final outcome delivered downstream
    iterations: int = 0                # how many agent invocations ran
    stopped_by: str = ""               # off | counter | match | validated | max_iter
    verdicts: list[str] = field(default_factory=list)  # judge outputs, when type==judge


async def run_agent_loop(
    loop: Optional[Loop],
    initial_task: str,
    run_agent: RunAgent,
    *,
    run_judge: Optional[RunJudge] = None,
) -> LoopResult:
    """Drive the outer loop for one agent node (§8.4).

    ``loop is None`` or ``loop.type == 'off'`` runs the agent exactly once. Otherwise the
    type dictates the stop condition; ``max_iter`` is a HARD force-exit for the open-ended
    types (``expression``/``judge``). ``iteration_input`` selects same-vs-previous feed.

    Returns a ``LoopResult`` recording the final outcome, the iteration count, and which
    rule stopped the loop (for observability)."""
    if loop is None or loop.type == "off":
        outcome = await run_agent(initial_task)
        return LoopResult(outcome=outcome, iterations=1, stopped_by="off")

    if loop.type == "counter":
        # Bounded by n; nothing else can stop it. Feed same-vs-previous per option.
        outcome = ""
        task = initial_task
        for i in range(loop.n):
            if i > 0 and loop.iteration_input == "previous":
                task = outcome
            outcome = await run_agent(task)
        return LoopResult(outcome=outcome, iterations=loop.n, stopped_by="counter")

    if loop.type == "expression":
        outcome = ""
        task = initial_task
        for i in range(loop.max_iter):
            if i > 0 and loop.iteration_input == "previous":
                task = outcome
            outcome = await run_agent(task)
            if expression_matches(loop.expression, outcome):
                return LoopResult(
                    outcome=outcome, iterations=i + 1, stopped_by="match"
                )
        # Hard force-exit: the expression never matched within the cap.
        log.warning(
            "loop 'expression' force-exited at max_iter=%d without a match", loop.max_iter
        )
        return LoopResult(outcome=outcome, iterations=loop.max_iter, stopped_by="max_iter")

    if loop.type == "judge":
        if loop.judge is None:  # guarded by DSL validation, but never trust silently
            raise RuntimeError("loop type 'judge' has no embedded judge")
        if run_judge is None:
            raise RuntimeError("loop type 'judge' requires a run_judge callable")
        judge = loop.judge
        outcome = ""
        verdicts: list[str] = []
        task = initial_task
        for i in range(loop.max_iter):
            if i > 0 and loop.iteration_input == "previous":
                task = outcome
            outcome = await run_agent(task)
            judge_task = _judge_task(judge, outcome, initial_task)
            verdict_text = await run_judge(judge, judge_task)
            verdicts.append(verdict_text)
            if verdict_validates(judge.verdict, verdict_text):
                return LoopResult(
                    outcome=outcome, iterations=i + 1, stopped_by="validated",
                    verdicts=verdicts,
                )
        # Hard force-exit: the Judge never validated within the cap.
        log.warning(
            "loop 'judge' force-exited at max_iter=%d without validation", loop.max_iter
        )
        return LoopResult(
            outcome=outcome, iterations=loop.max_iter, stopped_by="max_iter",
            verdicts=verdicts,
        )

    raise RuntimeError(f"unknown loop type '{loop.type}'")  # unreachable (Literal-typed)


def _judge_task(judge: Judge, outcome: str, original_input: str) -> str:
    """Build the text handed to the Judge. With a template, ``{outcome}`` binds the agent's
    latest outcome and ``{input}`` the original task; without one, the outcome is passed
    verbatim (the common case). A template referencing an unknown var fails loud."""
    if not judge.input_template:
        return outcome
    try:
        return judge.input_template.format(outcome=outcome, input=original_input)
    except KeyError as exc:
        raise RuntimeError(
            f"judge.input_template references missing var {exc}"
        ) from exc
