"""Phase 07 — outer loop-type execution (block_management.md §8.4).

Covers the phase exit criteria (implementation_plan/07):
  * off runs the agent exactly once;
  * counter runs exactly n;
  * expression stops on match AND force-exits at the max_iter cap;
  * judge stops when the embedded Judge validates AND force-exits at the cap;
  * the verdict-read option works BOTH ways (expression on the Judge output / structured field);
  * iteration_input feeds the same original input vs. reinserts the previous outcome.

Pure unit tests on run_agent_loop with mocked agent/judge callables — no live services
(the outer loop is deliberately free of runner/agent_server wiring, §8.4).
"""

import pytest

from agent_runtime.dsl import Judge, Loop, Verdict
from agent_runtime.nodes.loop import (
    expression_matches,
    run_agent_loop,
    verdict_validates,
)


# --- Mock invocation callables -------------------------------------------------
class AgentSpy:
    """Records every task it is asked and returns a scripted (or default) outcome."""

    def __init__(self, outcomes=None, default="outcome"):
        self._outcomes = list(outcomes) if outcomes is not None else None
        self._default = default
        self.tasks: list[str] = []
        self.calls = 0

    async def __call__(self, task: str) -> str:
        self.tasks.append(task)
        self.calls += 1
        if self._outcomes is not None:
            return self._outcomes.pop(0)
        return self._default


class JudgeSpy:
    """Returns scripted verdict texts; records the tasks it judged."""

    def __init__(self, verdicts):
        self._verdicts = list(verdicts)
        self.tasks: list[str] = []

    async def __call__(self, judge: Judge, task: str) -> str:
        self.tasks.append(task)
        return self._verdicts.pop(0)


# --- off -----------------------------------------------------------------------
async def test_off_runs_once():
    agent = AgentSpy(default="only")
    res = await run_agent_loop(Loop(type="off"), "task", agent)
    assert agent.calls == 1
    assert res.outcome == "only"
    assert res.iterations == 1
    assert res.stopped_by == "off"


async def test_none_loop_runs_once():
    agent = AgentSpy(default="only")
    res = await run_agent_loop(None, "task", agent)
    assert agent.calls == 1
    assert res.stopped_by == "off"


# --- counter -------------------------------------------------------------------
async def test_counter_runs_exactly_n():
    agent = AgentSpy(outcomes=["a", "b", "c"])
    res = await run_agent_loop(Loop(type="counter", n=3), "task", agent)
    assert agent.calls == 3
    assert res.iterations == 3
    assert res.outcome == "c"
    assert res.stopped_by == "counter"


async def test_counter_iteration_input_same_vs_previous():
    # same: every iteration sees the ORIGINAL task.
    same = AgentSpy(outcomes=["o1", "o2", "o3"])
    await run_agent_loop(
        Loop(type="counter", n=3, iteration_input="same"), "orig", same
    )
    assert same.tasks == ["orig", "orig", "orig"]

    # previous: iterations after the first see the PRIOR outcome.
    prev = AgentSpy(outcomes=["o1", "o2", "o3"])
    await run_agent_loop(
        Loop(type="counter", n=3, iteration_input="previous"), "orig", prev
    )
    assert prev.tasks == ["orig", "o1", "o2"]


# --- expression ----------------------------------------------------------------
async def test_expression_stops_on_match():
    agent = AgentSpy(outcomes=["nope", "still no", "DONE here", "unused"])
    res = await run_agent_loop(
        Loop(type="expression", expression="DONE", max_iter=10), "task", agent
    )
    assert res.iterations == 3
    assert res.stopped_by == "match"
    assert res.outcome == "DONE here"
    assert agent.calls == 3


async def test_expression_force_exits_at_cap():
    agent = AgentSpy(default="never matches")
    res = await run_agent_loop(
        Loop(type="expression", expression="DONE", max_iter=4), "task", agent
    )
    assert agent.calls == 4
    assert res.iterations == 4
    assert res.stopped_by == "max_iter"


async def test_expression_regex_form():
    agent = AgentSpy(outcomes=["draft", "final v2 ready"])
    res = await run_agent_loop(
        Loop(type="expression", expression=r"/v\d+/", max_iter=5), "task", agent
    )
    assert res.stopped_by == "match"
    assert res.iterations == 2


def test_expression_matches_substring_and_regex():
    assert expression_matches("DONE", "all DONE now") is True
    assert expression_matches("DONE", "nope") is False
    assert expression_matches(r"/^ok$/", "ok") is True
    assert expression_matches(r"/^ok$/", "not ok") is False


async def test_expression_iteration_input_previous():
    agent = AgentSpy(outcomes=["a", "b", "STOP"])
    await run_agent_loop(
        Loop(type="expression", expression="STOP", max_iter=5,
             iteration_input="previous"),
        "orig", agent,
    )
    assert agent.tasks == ["orig", "a", "b"]


# --- judge: verdict read = expression -----------------------------------------
async def test_judge_stops_when_validated_expression():
    agent = AgentSpy(outcomes=["v1", "v2", "v3"])
    judge = JudgeSpy(verdicts=["FAIL: redo", "FAIL: redo", "PASS looks good"])
    loop = Loop(
        type="judge", max_iter=10,
        judge=Judge(persona="critic", verdict=Verdict(read="expression", expression="PASS")),
    )
    res = await run_agent_loop(loop, "task", agent, run_judge=judge)
    assert res.iterations == 3
    assert res.stopped_by == "validated"
    assert res.outcome == "v3"
    assert res.verdicts == ["FAIL: redo", "FAIL: redo", "PASS looks good"]


async def test_judge_force_exits_at_cap():
    agent = AgentSpy(default="draft")
    judge = JudgeSpy(verdicts=["FAIL"] * 3)
    loop = Loop(
        type="judge", max_iter=3,
        judge=Judge(persona="critic", verdict=Verdict(read="expression", expression="PASS")),
    )
    res = await run_agent_loop(loop, "task", agent, run_judge=judge)
    assert agent.calls == 3
    assert res.iterations == 3
    assert res.stopped_by == "max_iter"


# --- judge: verdict read = structured field -----------------------------------
async def test_judge_verdict_read_field():
    agent = AgentSpy(outcomes=["draft1", "draft2"])
    judge = JudgeSpy(verdicts=[
        '{"result": {"passed": false}}',
        '{"result": {"passed": true}}',
    ])
    loop = Loop(
        type="judge", max_iter=10,
        judge=Judge(persona="critic", verdict=Verdict(read="field", field="result.passed")),
    )
    res = await run_agent_loop(loop, "task", agent, run_judge=judge)
    assert res.stopped_by == "validated"
    assert res.iterations == 2
    assert res.outcome == "draft2"


def test_verdict_validates_both_ways():
    # expression
    ve = Verdict(read="expression", expression="APPROVED")
    assert verdict_validates(ve, "APPROVED by judge") is True
    assert verdict_validates(ve, "rejected") is False
    # field (dotted path)
    vf = Verdict(read="field", field="ok")
    assert verdict_validates(vf, '{"ok": true}') is True
    assert verdict_validates(vf, '{"ok": false}') is False
    # non-JSON output → not validated (continue looping), no crash
    assert verdict_validates(vf, "not json at all") is False
    # missing path → not validated
    assert verdict_validates(vf, '{"other": true}') is False


async def test_judge_iteration_input_previous_and_template():
    agent = AgentSpy(outcomes=["o1", "o2"])
    judge = JudgeSpy(verdicts=["no", "yes"])
    loop = Loop(
        type="judge", max_iter=5, iteration_input="previous",
        judge=Judge(
            persona="critic",
            verdict=Verdict(read="expression", expression="yes"),
            input_template="Grade: {outcome} (orig: {input})",
        ),
    )
    res = await run_agent_loop(loop, "orig", agent, run_judge=judge)
    # agent saw original then the previous outcome
    assert agent.tasks == ["orig", "o1"]
    # judge saw the templated task binding {outcome} and {input}
    assert judge.tasks == [
        "Grade: o1 (orig: orig)",
        "Grade: o2 (orig: orig)",
    ]
    assert res.stopped_by == "validated"


# --- DSL validation ------------------------------------------------------------
def test_expression_loop_requires_expression():
    with pytest.raises(ValueError):
        Loop(type="expression", expression="")


def test_judge_loop_requires_judge():
    with pytest.raises(ValueError):
        Loop(type="judge")


def test_verdict_field_requires_field():
    with pytest.raises(ValueError):
        Verdict(read="field", field="")


def test_counter_n_must_be_positive():
    with pytest.raises(ValueError):
        Loop(type="counter", n=0)
