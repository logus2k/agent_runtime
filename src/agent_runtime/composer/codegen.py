"""Codegen — generate adapter code from interface schemas (Phase 5).

The catalog knows ``out(A).schema`` and ``in(B).schema``, so a local LLM can WRITE the
adapter. Two flows, both callable from a block:

* **Transform** — **auto-spec'd** from the two port schemas (``spec_for_transform`` is
  pure — the graph already stated the requirement, so the human types ~nothing) → the
  LLM writes a small mapping function.
* **Tool** — **described intent** (behaviour isn't in any interface) → the LLM writes a
  small script → **published to MCP** → attachable to any Agent's ``tools`` config.

The LLM and the MCP publisher are **injected** (a ``CodegenLLM`` / ``ToolPublisher``),
so this module is pure structure + prompt-spec + validation and is fully unit-testable
with fakes. Real wiring is a thin adapter: ``CodegenLLM`` over ``agent_server_client``,
``ToolPublisher`` over ``mcp_client``. There is **no silent default** — calling without
an LLM raises, never a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from .schema import DataSchema


class CodegenError(Exception):
    """Codegen could not produce a usable artifact (empty output, no LLM, etc.)."""


@runtime_checkable
class CodegenLLM(Protocol):
    """A local-LLM client the codegen calls. One method, so a fake is trivial and the
    real adapter (agent_server preset) is thin."""

    async def generate(self, prompt: str) -> str: ...


@runtime_checkable
class ToolPublisher(Protocol):
    """Publishes a generated tool so MCP can serve it (real impl: mcp_client)."""

    async def publish(self, name: str, code: str) -> None: ...


@dataclass
class TransformArtifact:
    in_schema: dict[str, Any]
    out_schema: dict[str, Any]
    spec: str
    code: str
    language: str = "python"


@dataclass
class ToolArtifact:
    name: str
    intent: str
    spec: str
    code: str
    language: str = "python"
    published: bool = False


def spec_for_transform(in_schema: DataSchema, out_schema: DataSchema) -> str:
    """PURE: derive the codegen spec/prompt from the two port schemas. This is the
    'near-zero typing' promise — the requirement is fully implied by the wiring."""
    return (
        "Write a single Python function `transform(value)` that maps an input matching "
        "this JSON-Schema:\n"
        f"  INPUT  = {in_schema.to_json()}\n"
        "to an output matching this JSON-Schema:\n"
        f"  OUTPUT = {out_schema.to_json()}\n"
        "Return only the function body/definition. It must be pure (no I/O), total "
        "(handle missing optional fields), and deterministic."
    )


def spec_for_tool(intent: str, name: str) -> str:
    """PURE: derive the codegen spec/prompt for a tool from a described intent."""
    if not intent or not intent.strip():
        raise CodegenError("tool intent must be a non-empty description")
    return (
        f"Write a single Python function `{name}(**kwargs)` implementing this intent:\n"
        f"  {intent.strip()}\n"
        "Return only the function definition. Keep it small and dependency-light so a "
        "local model can produce it; it will be published as an MCP tool."
    )


async def generate_transform(
    in_schema: DataSchema,
    out_schema: DataSchema,
    *,
    llm: Optional[CodegenLLM] = None,
) -> TransformArtifact:
    """Auto-spec from the port schemas, then have the LLM write the mapping."""
    if llm is None:
        raise CodegenError(
            "generate_transform requires an injected CodegenLLM (agent_server adapter)"
        )
    spec = spec_for_transform(in_schema, out_schema)
    code = await llm.generate(spec)
    if not code or not code.strip():
        raise CodegenError("LLM returned empty transform code")
    return TransformArtifact(
        in_schema=in_schema.to_json(),
        out_schema=out_schema.to_json(),
        spec=spec,
        code=code,
    )


async def generate_tool(
    intent: str,
    *,
    name: str,
    llm: Optional[CodegenLLM] = None,
    publisher: Optional[ToolPublisher] = None,
) -> ToolArtifact:
    """From a described intent, have the LLM write a tool; publish to MCP if a
    publisher is provided (otherwise return it unpublished for review)."""
    if llm is None:
        raise CodegenError(
            "generate_tool requires an injected CodegenLLM (agent_server adapter)"
        )
    spec = spec_for_tool(intent, name)
    code = await llm.generate(spec)
    if not code or not code.strip():
        raise CodegenError("LLM returned empty tool code")
    published = False
    if publisher is not None:
        await publisher.publish(name, code)
        published = True
    return ToolArtifact(name=name, intent=intent, spec=spec, code=code, published=published)
