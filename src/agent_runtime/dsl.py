"""The runtime DSL — Pydantic models for a (flat) agent record (v0).

This is the contract agent_runtime executes. It describes **structure, parameters,
and references** only — never cognition (prompts live in agent_server presets;
tool logic lives in MCP). See documents/runtime_dsl_specification.md.

Validation policy (decided with António):
  * ``extra="forbid"`` everywhere — an unknown/typo'd field is a hard error, not a
    silently-dropped key. (No silent failures.)
  * ``version`` is ``major.minor``; the runtime accepts only known majors and
    rejects unknown ones with a clear message. Minor migration is a compiler
    concern, not the runtime's.
  * ``tools.allow`` is validated for **shape** only (``<server>__<tool>``), NOT for
    existence on a live MCP server — that is a connect-time check, so loading stays
    pure/offline.

v0 implements the flat record (the degenerate linear graph); the graph form in the
spec is forward-looking and not modelled yet.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Supported DSL major version. A record whose major differs is rejected.
SUPPORTED_MAJOR = 0

# A namespaced tool name: <server>__<tool>, e.g. noted__web_search. The client
# applies the server prefix; the raw name is sent to MCP tools/call.
_TOOL_RE = re.compile(r"^[A-Za-z0-9]+__[A-Za-z0-9_]+$")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)$")


class _Strict(BaseModel):
    """Base: forbid unknown fields so typos fail loudly at load time."""

    model_config = ConfigDict(extra="forbid")


class Trigger(_Strict):
    type: Literal["schedule", "channel"] = "schedule"


class LLM(_Strict):
    """Optional sampling overrides, merged into the agent_server request."""

    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    min_p: Optional[float] = None

    def as_overrides(self) -> dict[str, Any]:
        """Only the set fields, for merging into a chat request."""
        return {k: v for k, v in self.model_dump().items() if v is not None}


class Brain(_Strict):
    persona: str  # agent_server preset name (existence verified at compile time)
    llm: LLM = Field(default_factory=LLM)

    @field_validator("persona")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("brain.persona must be a non-empty preset name")
        return v


class Tools(_Strict):
    server: str  # MCP server key (resolved to a URL by the runtime's config)
    allow: list[str] = Field(default_factory=list)
    max_rounds: int = 3

    @field_validator("allow")
    @classmethod
    def _validate_tool_shape(cls, v: list[str]) -> list[str]:
        bad = [name for name in v if not _TOOL_RE.match(name)]
        if bad:
            raise ValueError(
                f"tools.allow entries must match <server>__<tool> "
                f"(e.g. noted__web_search); offending: {bad}"
            )
        return v

    @field_validator("max_rounds")
    @classmethod
    def _positive_rounds(cls, v: int) -> int:
        if v < 1:
            raise ValueError("tools.max_rounds must be >= 1")
        return v


class Rag(_Strict):
    rewriter: Optional[str] = None
    domains: list[str] = Field(default_factory=list)
    use_graph: bool = False


class Guardrails(_Strict):
    forbidden: list[str] = Field(default_factory=list)
    min_confidence: Optional[float] = None


class Input(_Strict):
    template: str = ""
    vars: dict[str, Any] = Field(default_factory=dict)


class Memory(_Strict):
    policy: Literal["none", "thread_window"] = "none"
    max_turns: int = 20


class Delivery(_Strict):
    channel: Literal["whatsapp", "bus", "tts"]
    target: str

    @field_validator("target")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("delivery.target must be non-empty")
        return v


class AgentRecord(_Strict):
    """A complete agent definition (flat record form). Required: version, uid, name,
    brain, delivery. trigger defaults to a schedule trigger.

    Identity is the immutable ``uid`` (a UUID, server-assigned on create); ``name`` is a
    human-friendly label that can change without affecting routing — the routing key is
    always the uid. See documents/administration_frontend.md §4.0."""

    version: str
    uid: str
    name: str
    description: Optional[str] = None
    trigger: Trigger = Field(default_factory=Trigger)
    brain: Brain
    tools: Optional[Tools] = None
    rag: Optional[Rag] = None
    guardrails: Optional[Guardrails] = None
    input: Input = Field(default_factory=Input)
    memory: Memory = Field(default_factory=Memory)
    delivery: Delivery

    @field_validator("uid")
    @classmethod
    def _uid_shape(cls, v: str) -> str:
        try:
            uuid.UUID(str(v))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError(f"uid '{v}' must be a UUID string") from exc
        return v

    @field_validator("name")
    @classmethod
    def _name_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("name must be a non-empty label")
        return v

    @field_validator("version")
    @classmethod
    def _known_major(cls, v: str) -> str:
        m = _VERSION_RE.match(v)
        if not m:
            raise ValueError(f"version '{v}' must be 'major.minor' (e.g. '0.1')")
        major = int(m.group(1))
        if major != SUPPORTED_MAJOR:
            raise ValueError(
                f"unsupported DSL major version '{v}': this runtime supports "
                f"major {SUPPORTED_MAJOR}.x only"
            )
        return v
