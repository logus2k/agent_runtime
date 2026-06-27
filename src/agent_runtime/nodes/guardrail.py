"""The guardrail node (Proxy) — local output checks before delivery.

Because LLM failure is probabilistic, this is a first-class node, not decoration:
it is where hallucination/forbidden-output blast radius is contained. A failed
guardrail routes to a rejected path (the caller decides what to do — log, escalate,
suppress delivery) and is never silently ignored.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..dsl import Guardrails


@dataclass
class GuardrailResult:
    ok: bool
    reason: str | None = None


def apply_guardrails(
    guardrails: Guardrails | None,
    output: str,
    *,
    confidence: float | None = None,
) -> GuardrailResult:
    """Check ``output`` against the record's guardrails. Returns ok=False with a
    human-readable reason on the first violation."""
    if guardrails is None:
        return GuardrailResult(ok=True)

    for pattern in guardrails.forbidden:
        if pattern and pattern in output:
            return GuardrailResult(ok=False, reason=f"forbidden pattern present: {pattern!r}")

    if guardrails.min_confidence is not None and confidence is not None:
        if confidence < guardrails.min_confidence:
            return GuardrailResult(
                ok=False,
                reason=f"confidence {confidence} below min {guardrails.min_confidence}",
            )

    return GuardrailResult(ok=True)
