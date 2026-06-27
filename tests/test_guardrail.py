"""Step 3: guardrail node — pure local checks (no services)."""

from agent_runtime.dsl import Guardrails
from agent_runtime.nodes.guardrail import apply_guardrails


def test_none_guardrails_pass():
    assert apply_guardrails(None, "anything").ok


def test_forbidden_pattern_blocks():
    g = Guardrails(forbidden=["rm -rf", "DROP TABLE"])
    r = apply_guardrails(g, "please run rm -rf / now")
    assert not r.ok
    assert "rm -rf" in r.reason


def test_clean_output_passes():
    g = Guardrails(forbidden=["rm -rf"])
    assert apply_guardrails(g, "here are your headlines").ok


def test_low_confidence_blocks():
    g = Guardrails(min_confidence=0.6)
    assert not apply_guardrails(g, "ok", confidence=0.4).ok
    assert apply_guardrails(g, "ok", confidence=0.9).ok


def test_missing_confidence_is_not_blocked():
    # min_confidence set but no confidence supplied -> can't evaluate, don't block
    g = Guardrails(min_confidence=0.6)
    assert apply_guardrails(g, "ok").ok
