"""Regression tests for the farm's consume-loop health signal — ALWAYS RUN (pure dependency
injection: a fake bus, no live Valkey), so a broken consumer can never ship.

The bug this guards: the old consume loop logged ``NOGROUP`` and continued forever — the farm
stayed "up/healthy" while silently consuming nothing (fired events unread → no runs → requests
never reach the Agent). The ROOT cause (a key-TTL on the farm's stream reaping the consumer
group when idle) is fixed in the bus SDK (streams are MAXLEN-bounded, never key-TTL'd). Here we
pin the loud-failure contract: a missing group is NOT silently recreated (that would mask a real
fault) — the farm marks NOT consuming so ``/health`` returns degraded (503) and the process is
restarted. These tests are the always-run guard for that contract.
"""

from __future__ import annotations

import asyncio
import dataclasses
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_runtime.config import Settings
from agent_runtime.farm import Farm
from agent_runtime.registry import Registry


def _registry(tmp_path: Path) -> Registry:
    (tmp_path / "noop.yaml").write_text(textwrap.dedent(
        """
        version: "0.1"
        uid: 00000000-0000-4000-8000-0000000000a1
        name: noop
        brain: { persona: p }
        delivery: { channel: bus, target: t }
        """), encoding="utf-8")
    reg = Registry(tmp_path)
    reg.load_all()
    return reg


def _settings() -> Settings:
    return dataclasses.replace(Settings(), poll_ms=5, farm_stream_id="it-heal", consumer_group="cg:it-heal")


async def _wait_until(pred, timeout=3.0, interval=0.02):
    elapsed = 0.0
    while elapsed < timeout:
        if pred():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return pred()


class _FakeBus:
    """The bus contract the consume loop touches. ``group_exists=False`` simulates the group
    having vanished (Valkey flushed after startup): ``read_group`` raises NOGROUP until
    ``ensure_group`` recreates it. ``ensure_fails`` simulates the bus being truly down."""

    def __init__(self, group_exists: bool = False, ensure_fails: bool = False) -> None:
        self.group_exists = group_exists
        self.ensure_fails = ensure_fails
        self.ensure_calls = 0
        self.read_calls = 0

    async def ensure_group(self, stream, group, start="0"):
        self.ensure_calls += 1
        if self.ensure_fails:
            raise RuntimeError("connection refused")   # bus unreachable
        self.group_exists = True

    async def read_group(self, streams, group, consumer, count=10, block_ms=None):
        self.read_calls += 1
        if not self.group_exists:
            raise RuntimeError(
                "NOGROUP: No such key 'stream:it-heal' or consumer group 'cg:it-heal' "
                "in XREADGROUP with GROUP option")
        return []   # group healthy → nothing pending

    async def reclaim(self, *a, **k):
        return ("0-0", [])

    async def ack(self, *a, **k):
        return None

    async def close(self):
        return None


async def _run_loop_briefly(farm: Farm, until, timeout=3.0):
    farm._running = True
    task = asyncio.create_task(farm._consume_loop())
    try:
        await _wait_until(until, timeout=timeout)
    finally:
        farm._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_missing_group_marks_degraded_and_is_NOT_recreated(tmp_path):
    # A missing group must NOT be silently recreated (that masks a real fault). The loop marks
    # NOT consuming so /health reports degraded; it never calls ensure_group to self-heal.
    fake = _FakeBus(group_exists=False)
    farm = Farm(_settings(), _registry(tmp_path), lambda r, e: None, bus=fake)
    await _run_loop_briefly(farm, lambda: not farm.consuming() and fake.read_calls >= 1)
    assert farm.consuming() is False      # surfaced as degraded, not silently ok
    assert fake.ensure_calls == 0         # did NOT recreate the group (no silent self-heal)


async def test_healthy_read_marks_consuming(tmp_path):
    # A group that exists → reads succeed → consuming() is True (health ok).
    fake = _FakeBus(group_exists=True)
    farm = Farm(_settings(), _registry(tmp_path), lambda r, e: None, bus=fake)
    await _run_loop_briefly(farm, lambda: fake.read_calls >= 1)
    assert farm.consuming() is True
    assert fake.ensure_calls == 0


async def test_health_endpoint_reflects_consuming():
    # /health returns degraded/503 when the farm isn't consuming, ok/200 when it is.
    from agent_runtime import app as app_module

    def _req(consuming):
        farm = SimpleNamespace(consuming=lambda: consuming)
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(farm=farm)))

    ok = await app_module.health(_req(True))
    assert ok.status_code == 200
    degraded = await app_module.health(_req(False))
    assert degraded.status_code == 503
