"""agent_server client — the reasoning backend (OpenAI-compatible).

Grounded against the live agent_server (http://agent_server:7701):
``POST /v1/chat/completions`` with ``model`` = a preset name (persona). Returns
``choices[0].message`` carrying ``content`` (which may embed ``<think>…</think>``)
and optional ``tool_calls`` in standard OpenAI shape.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("agent_runtime.agent_server")


class AgentServerError(Exception):
    """A chat-completions call failed (transport or non-200)."""


class AgentServerClient:
    def __init__(self, base_url: str, *, timeout_s: float = 90.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One chat-completions round. Returns the assistant message dict
        (``{role, content, tool_calls?}``)."""
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if tools:
            payload["tools"] = tools
        if overrides:
            payload.update(overrides)

        url = f"{self._base}/v1/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise AgentServerError(f"agent_server transport error to {url}: {exc}") from exc
        if resp.status_code != 200:
            raise AgentServerError(
                f"agent_server '{model}' -> HTTP {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise AgentServerError(f"agent_server '{model}' returned no choices: {data}")
        return choices[0].get("message") or {}
