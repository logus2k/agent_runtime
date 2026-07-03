"""In-process event hub — a push fan-out of run events to live SSE subscribers.

The Runner publishes every run event here (in addition to the bus runs stream); the admin
SSE endpoint (``GET /admin/projects/<uid>/events``) subscribes one queue per connection and
streams the matching events to Patron's Console (Receive) panel. This is a pure in-memory,
single-process fan-out — no polling, low latency. A slow subscriber drops events (bounded
queue) rather than back-pressuring a workflow run.
"""

from __future__ import annotations

import asyncio
from typing import Any


class EventHub:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> "asyncio.Queue[dict[str, Any]]":
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[dict[str, Any]]") -> None:
        self._subs.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # drop for a slow subscriber; never block the run


# Process-global singleton (the Runner and the admin SSE endpoint share it by import).
hub = EventHub()
