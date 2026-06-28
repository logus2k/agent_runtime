"""Step 2: the farm consumes a synthetic trigger and dispatches one bounded task.

Needs a live Valkey (VALKEY_HOST/PORT, default 127.0.0.1:6379); SKIPS with a clear
message otherwise (no silent pass). Uses unique stream/group names per run so it
never collides with a running farm.
"""

import asyncio
import dataclasses
import os
import textwrap
import uuid
from pathlib import Path

import pytest

pytest.importorskip("glide", reason="valkey-glide not installed ([bus] extra)")

from agent_bus_client import new_event  # noqa: E402

from agent_runtime.config import Settings  # noqa: E402
from agent_runtime.farm import Farm  # noqa: E402
from agent_runtime.registry import Registry  # noqa: E402

# Dedicated test vars so the container-oriented .env (VALKEY_HOST=valkey-bus, which
# only resolves inside the docker network) can't leak in and make us hang trying to
# reach a service name from the host. Default to the host-published loopback.
HOST = os.getenv("VALKEY_TEST_HOST", "127.0.0.1")
PORT = int(os.getenv("VALKEY_TEST_PORT", "6379"))


def _noop_registry(tmp_path: Path) -> Registry:
    (tmp_path / "noop.yaml").write_text(
        textwrap.dedent(
            """
            version: "0.1"
            uid: 00000000-0000-4000-8000-0000000000a1
            name: noop
            brain: { persona: p }
            delivery: { channel: bus, target: t }
            """
        ),
        encoding="utf-8",
    )
    reg = Registry(tmp_path)
    reg.load_all()
    return reg


async def _wait_until(predicate, timeout=5.0, interval=0.05):
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return predicate()


async def test_farm_dispatch_idempotency_and_routing(tmp_path):
    tag = uuid.uuid4().hex[:8]
    settings = dataclasses.replace(
        Settings(),
        valkey_host=HOST,
        valkey_port=PORT,
        farm_stream_id=f"it-{tag}",
        consumer_group=f"cg:it-{tag}",
        poll_ms=100,
        reaper_interval_s=1,
        reaper_min_idle_ms=200,
        dedupe_ttl_s=60,
        connect_retries=2,        # fail fast if Valkey is down (don't hang the suite)
        connect_retry_delay_s=1,
    )
    registry = _noop_registry(tmp_path)

    calls: list[str] = []

    async def handler(record, env):
        # the resolved record + event reach the pipeline (routed by name fallback)
        assert record.name == "noop"
        calls.append(env.header.cid)

    farm = Farm(settings, registry, handler)
    try:
        await farm.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no live Valkey at {HOST}:{PORT}: {exc}")

    stream = settings.farm_stream_key()
    # Unique cids per run: the idempotency dedupe key (ar:dedupe:<cid>:<sid>) persists
    # in Valkey for dedupe_ttl_s, so fixed cids would falsely dedupe across runs.
    c1, c2, c3 = f"wf1-{tag}", f"wf2-{tag}", f"wf3-{tag}"
    try:
        await farm.start()

        # 1) a valid trigger runs the agent exactly once
        await farm.bus.publish(
            stream,
            new_event(stream_id=f"it-{tag}", cid=c1, sid=1, sender="test",
                      event_type="schedule.fired", data={"agent": "noop"}),
        )
        assert await _wait_until(lambda: calls == [c1]), f"calls={calls}"

        # 2) at-least-once: a redelivery of the same cid+sid is deduped (not re-run)
        await farm.bus.publish(
            stream,
            new_event(stream_id=f"it-{tag}", cid=c1, sid=1, sender="test",
                      event_type="schedule.fired", data={"agent": "noop"}),
        )
        await asyncio.sleep(0.6)
        assert calls == [c1], f"duplicate was re-run: {calls}"

        # 3) an unknown agent id is acked + logged, never dispatched
        await farm.bus.publish(
            stream,
            new_event(stream_id=f"it-{tag}", cid=c2, sid=1, sender="test",
                      event_type="schedule.fired", data={"agent": "ghost"}),
        )
        await asyncio.sleep(0.6)
        assert calls == [c1], f"unknown agent dispatched: {calls}"

        # 4) a second distinct trigger runs again
        await farm.bus.publish(
            stream,
            new_event(stream_id=f"it-{tag}", cid=c3, sid=1, sender="test",
                      event_type="schedule.fired", data={"agent": "noop"}),
        )
        assert await _wait_until(lambda: calls == [c1, c3]), f"calls={calls}"
    finally:
        await farm.bus.client.delete([stream])
        await farm.stop()
