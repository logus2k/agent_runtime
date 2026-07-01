"""Phase 5: the codegen loop (Transform auto-spec'd from schemas; Tool from intent).

Uses fake LLM/publisher so the structure is verified offline. The key property: the
Transform spec is DERIVED from the two port schemas (near-zero human input), and a
generated tool can be published to MCP. No-LLM calls raise loudly (no silent no-op).
"""

import pytest

from agent_runtime.composer.codegen import (
    CodegenError,
    generate_tool,
    generate_transform,
    spec_for_tool,
    spec_for_transform,
)
from agent_runtime.composer.schema import DataSchema, STRING


class FakeLLM:
    def __init__(self, code: str):
        self.code = code
        self.prompts: list[str] = []

    async def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.code


class FakePublisher:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, name: str, code: str) -> None:
        self.published.append((name, code))


def test_transform_spec_is_derived_from_both_port_schemas():
    obj = DataSchema(type="object", properties={"n": DataSchema(type="integer")})
    spec = spec_for_transform(STRING, obj)
    assert "INPUT" in spec and "OUTPUT" in spec
    assert "string" in spec           # the input schema
    assert "object" in spec           # the output schema


async def test_generate_transform_uses_the_spec_and_wraps_code():
    llm = FakeLLM("def transform(value):\n    return {'n': len(value)}")
    art = await generate_transform(STRING, DataSchema(type="object"), llm=llm)
    assert art.code.startswith("def transform")
    assert art.spec in llm.prompts          # the LLM was called with the derived spec
    assert art.in_schema == {"type": "string"}


async def test_generate_tool_publishes_to_mcp_when_publisher_given():
    llm = FakeLLM("def weather(**kwargs):\n    return {'temp': 21}")
    pub = FakePublisher()
    art = await generate_tool("query the weather for a city", name="weather", llm=llm, publisher=pub)
    assert art.published is True
    assert pub.published == [("weather", art.code)]


async def test_generate_tool_without_publisher_returns_unpublished():
    llm = FakeLLM("def weather(**kwargs):\n    return {}")
    art = await generate_tool("query weather", name="weather", llm=llm)
    assert art.published is False


async def test_empty_llm_output_raises():
    with pytest.raises(CodegenError, match="empty"):
        await generate_transform(STRING, STRING, llm=FakeLLM("   "))


async def test_no_llm_raises_loudly_not_silent_noop():
    with pytest.raises(CodegenError, match="requires an injected"):
        await generate_transform(STRING, STRING)
    with pytest.raises(CodegenError, match="requires an injected"):
        await generate_tool("x", name="x")


def test_empty_intent_rejected():
    with pytest.raises(CodegenError, match="non-empty"):
        spec_for_tool("  ", "t")
