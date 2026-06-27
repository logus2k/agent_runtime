"""Step 4/5: full pipeline end-to-end (minus WhatsApp).

Synthetic trigger -> farm -> runner -> brain (live agent_server + mcp-service
web_search) -> BUS delivery. Verifies the curated result lands on the target stream
and that run events (tool.exec/agent.result/workflow.terminated) are emitted.

Needs the live stack (valkey + agent_server + mcp-service) on host loopback; SKIPS
loudly otherwise. Uses BUS delivery (not WhatsApp) so nothing is sent to a real chat.
"""

import asyncio
import dataclasses
import os
import textwrap
import uuid
from pathlib import Path

import httpx
import pytest

pytest.importorskip("glide", reason="valkey-glide not installed ([bus] extra)")

from agent_bus_client import new_event  # noqa: E402

from agent_runtime.config import Settings  # noqa: E402
from agent_runtime.farm import Farm  # noqa: E402
from agent_runtime.registry import Registry  # noqa: E402

VALKEY_HOST = os.getenv("VALKEY_TEST_HOST", "127.0.0.1")
VALKEY_PORT = int(os.getenv("VALKEY_TEST_PORT", "6379"))
AGENT_SERVER = os.getenv("AGENT_SERVER_TEST_URL", "http://127.0.0.1:7701")
MCP = os.getenv("MCP_TEST_URL", "http://127.0.0.1:4950/mcp/")
PRESET = os.getenv("BRAIN_TEST_PRESET", "general")


async def _services_up() -> str | None:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.get(f"{AGENT_SERVER}/health")
            await c.post(MCP, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                    "params": {}},
                         headers={"Accept": "application/json, text/event-stream"})
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return None


async def _wait_for_result(bus, stream, timeout=60.0):
    elapsed = 0.0
    while elapsed < timeout:
        _cursor, envs = await bus.observe(stream, "0", count=50)
        for e in envs:
            if e.header.event_type == "agent.result":
                return e
        await asyncio.sleep(0.5)
        elapsed += 0.5
    return None


async def test_full_pipeline_bus_delivery(tmp_path):
    down = await _services_up()
    if down:
        pytest.skip(f"live stack not reachable: {down}")

    tag = uuid.uuid4().hex[:8]
    out_id = f"newsout-{tag}"
    (tmp_path / "news-e2e.yaml").write_text(
        textwrap.dedent(
            f"""
            version: "0.1"
            id: news-e2e
            brain: {{ persona: {PRESET}, llm: {{ temperature: 0.3, max_tokens: 512 }} }}
            tools: {{ server: mcp, allow: [mcp__web_search], max_rounds: 4 }}
            input:
              template: "Use the mcp__web_search tool to search for {{topic}}, then list the top 3 result titles as a short bulleted list. Call the tool before answering."
              vars: {{ topic: "AI agents" }}
            delivery: {{ channel: bus, target: {out_id} }}
            """
        ),
        encoding="utf-8",
    )
    registry = Registry(tmp_path)
    registry.load_all()

    settings = dataclasses.replace(
        Settings(),
        valkey_host=VALKEY_HOST,
        valkey_port=VALKEY_PORT,
        agent_server_url=AGENT_SERVER,
        mcp_url=MCP,
        mcp_server_key="mcp",
        farm_stream_id=f"farm-{tag}",
        consumer_group=f"cg:{tag}",
        runs_stream_id=f"runs-{tag}",
        poll_ms=100,
        connect_retries=2,
        connect_retry_delay_s=1,
    )

    farm = Farm(settings, registry)
    await farm.connect()
    runner_stream = settings.farm_stream_key()
    out_stream = farm.bus.stream_key(out_id)
    runs_stream = farm.bus.stream_key(settings.runs_stream_id)
    try:
        from agent_runtime.runner import Runner

        farm.set_handler(Runner(settings, farm.bus).run)
        await farm.start()

        await farm.bus.publish(
            runner_stream,
            new_event(stream_id=f"farm-{tag}", cid=f"wf-{tag}", sid=1, sender="test",
                      event_type="schedule.fired", data={"agent": "news-e2e"}),
        )

        result = await _wait_for_result(farm.bus, out_stream, timeout=90)
        assert result is not None, "no agent.result delivered to the bus stream"
        output = result.payload.data.get("output", "")
        assert output.strip(), f"empty delivered output: {result.payload.data}"
        print("\n=== DELIVERED OUTPUT ===\n" + output[:600])

        # run events were emitted on the runs stream
        _c, run_envs = await farm.bus.observe(runs_stream, "0", count=100)
        types = [e.header.event_type for e in run_envs]
        assert "tool.exec" in types, f"no tool.exec emitted; got {types}"
        assert "agent.result" in types, f"no agent.result emitted; got {types}"
        assert "workflow.terminated" in types, f"no terminal event; got {types}"
        print("=== RUN EVENTS ===", types)
    finally:
        for st in (runner_stream, out_stream, runs_stream):
            try:
                await farm.bus.client.delete([st])
            except Exception:  # noqa: BLE001
                pass
        await farm.stop()
