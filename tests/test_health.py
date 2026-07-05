"""Step 0 verification: the /health route returns ok.

We call the route coroutine directly rather than via TestClient — the app lifespan
now boots the farm (connects to Valkey), which a pure unit test must not require.
``health`` reads the farm's consume-liveness off ``request.app.state.farm``; a farm that is
consuming (or absent, in this bare probe) is ``ok``/200.
"""

import asyncio
import json
from types import SimpleNamespace

from agent_runtime.app import health


def _req(farm=None):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(farm=farm)))


def test_health_ok():
    # No farm on state (bare probe) → treated as ok/200.
    resp = asyncio.run(health(_req()))
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["status"] == "ok"
    assert body["service"] == "agent_runtime"


def test_health_degraded_when_not_consuming():
    resp = asyncio.run(health(_req(SimpleNamespace(consuming=lambda: False))))
    assert resp.status_code == 503
    assert json.loads(resp.body)["status"] == "degraded"
