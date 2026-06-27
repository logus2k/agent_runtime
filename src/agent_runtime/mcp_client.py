"""MCP client — talks to an MCP server over Streamable HTTP (JSON-RPC).

Grounded against the live noted MCP server (http://noted:8123/mcp/): it is
**stateless** (no initialize handshake needed) and returns a plain JSON response.
  * ``tools/list`` → ``result.tools[]`` each ``{name, description, inputSchema}``
  * ``tools/call`` {name, arguments} → ``result.content[].text`` (+ ``isError``)

Tool names on the wire are **raw** (``web_search``); the DSL allow-list and the
specs we advertise to the LLM are **namespaced** (``noted__web_search``). This
client owns that mapping: it advertises prefixed names and strips the prefix when
calling the server. Errors are raised loudly, never swallowed.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("agent_runtime.mcp")

_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


class MCPError(Exception):
    """An MCP call failed (transport, JSON-RPC error, or tool isError)."""


class MCPClient:
    def __init__(self, url: str, server: str, *, timeout_s: float = 30.0):
        self._url = url
        self._server = server
        self._prefix = f"{server}__"
        self._timeout = timeout_s
        self._id = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._url, json=body, headers=_MCP_HEADERS)
        except httpx.HTTPError as exc:
            raise MCPError(f"MCP transport error to {self._url}: {exc}") from exc
        if resp.status_code != 200:
            raise MCPError(
                f"MCP {method} -> HTTP {resp.status_code}: {resp.text[:500]}"
            )
        payload = resp.json()
        if "error" in payload:
            raise MCPError(f"MCP {method} JSON-RPC error: {payload['error']}")
        return payload.get("result") or {}

    async def list_tools(self) -> list[dict[str, Any]]:
        """Raw tool specs from the server (unprefixed names)."""
        result = await self._rpc("tools/list", {})
        return result.get("tools", [])

    async def openai_tools(self, allow: list[str]) -> list[dict[str, Any]]:
        """Build OpenAI function specs for the allowed (prefixed) tool names.

        Raises if an allowed tool isn't on the server — a missing tool is a real
        misconfiguration, not something to silently drop."""
        by_raw = {t["name"]: t for t in await self.list_tools()}
        specs: list[dict[str, Any]] = []
        missing: list[str] = []
        for advertised in allow:
            raw = self._strip(advertised)
            spec = by_raw.get(raw)
            if spec is None:
                missing.append(advertised)
                continue
            specs.append(
                {
                    "type": "function",
                    "function": {
                        "name": advertised,  # advertise the namespaced name to the LLM
                        "description": spec.get("description", ""),
                        "parameters": spec.get("inputSchema", {"type": "object"}),
                    },
                }
            )
        if missing:
            raise MCPError(
                f"tools not found on MCP server '{self._server}': {missing} "
                f"(available: {sorted(by_raw)[:20]}…)"
            )
        return specs

    async def call(self, advertised_name: str, arguments: dict[str, Any]) -> str:
        """Invoke a tool (named with its advertised/prefixed name) and return its
        text content. Raises MCPError on a tool error."""
        raw = self._strip(advertised_name)
        result = await self._rpc("tools/call", {"name": raw, "arguments": arguments})
        text = self._content_text(result)
        if result.get("isError"):
            raise MCPError(f"tool '{advertised_name}' returned an error: {text[:500]}")
        return text

    def _strip(self, name: str) -> str:
        return name[len(self._prefix):] if name.startswith(self._prefix) else name

    @staticmethod
    def _content_text(result: dict[str, Any]) -> str:
        parts = result.get("content") or []
        texts = [p.get("text", "") for p in parts if p.get("type") == "text"]
        return "\n".join(texts).strip()
