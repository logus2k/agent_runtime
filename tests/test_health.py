"""Step 0 verification: the /health route returns ok.

We call the route coroutine directly rather than via TestClient — the app lifespan
now boots the farm (connects to Valkey), which a pure unit test must not require.
"""

import asyncio

from agent_runtime.app import health


def test_health_ok():
    body = asyncio.run(health())
    assert body["status"] == "ok"
    assert body["service"] == "agent_runtime"
