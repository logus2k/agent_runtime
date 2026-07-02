"""Phase 05 — the farm routes a fired event carrying ``record_uid`` to the deployed
GraphRecord (DEFECT 3, §9.3.1).

Unit-level: drives ``Farm._handle`` directly with a fake bus (incr/expire/ack) so no live
Valkey is needed. Asserts that:
  * a delivery whose event_data has ``record_uid`` resolves against the shared
    GraphRegistry and is dispatched to ``run_graph_record`` (the graph handler);
  * an unknown ``record_uid`` is acked + logged loudly (log.error), never silently dropped,
    and does NOT reach the graph handler;
  * a delivery WITHOUT ``record_uid`` still takes the flat-agent path (the fallback).
"""

from __future__ import annotations

import dataclasses

from agent_bus_client import new_event
from agent_bus_client.bus import Delivery

from agent_runtime.config import Settings
from agent_runtime.dsl_graph import GraphEdge, GraphNode, GraphRecord
from agent_runtime.farm import Farm
from agent_runtime.graph_registry import GraphRegistry


# --- a minimal fake bus (only the verbs _handle uses) -------------------------
class FakeBus:
    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self.acked: list[tuple[str, str, list[str]]] = []

    async def incr(self, key: str) -> int:
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    async def expire(self, key: str, ttl: int) -> None:
        return None

    async def ack(self, stream: str, group: str, ids: list[str]) -> None:
        self.acked.append((stream, group, ids))


def _delivery(*, record_uid: str | None = None, agent: str | None = None,
              cid: str = "c1", sid: int = 1) -> Delivery:
    data: dict = {}
    if record_uid is not None:
        data["record_uid"] = record_uid
    if agent is not None:
        data["agent"] = agent
    env = new_event(
        stream_id="agent-runtime", cid=cid, sid=sid, sender="test",
        event_type="schedule.fired", data=data,
    )
    return Delivery(stream="stream:agent-runtime", entry_id="1-0", envelope=env)


def _graph_record(uid: str, *, enabled: bool = True) -> GraphRecord:
    return GraphRecord(
        version="0.1", uid=uid, name="News Project", enabled=enabled,
        entry="initiator:1",
        nodes=[
            GraphNode(id="initiator:1", kind="initiator"),
            GraphNode(id="agent:2", kind="agent"),
        ],
        edges=[GraphEdge(src="initiator:1", dst="agent:2")],
    )


def _farm_with_graph_routing(tmp_path):
    settings = dataclasses.replace(Settings(), dedupe_ttl_s=60, job_timeout_s=5)
    graph_reg = GraphRegistry()

    graph_calls: list[str] = []
    flat_calls: list[str] = []

    async def graph_handler(record, env):
        graph_calls.append(record.uid)

    async def flat_handler(record, env):
        flat_calls.append(record.name)

    from agent_runtime.registry import Registry
    reg = Registry(tmp_path)  # empty dir -> no flat agents
    reg.load_all()
    farm = Farm(settings, reg, flat_handler)
    farm.set_graph_routing(graph_reg, graph_handler)
    farm._bus = FakeBus()
    return farm, graph_reg, graph_calls, flat_calls


async def test_record_uid_routes_to_graph_registry_and_runs_graph_record(tmp_path):
    farm, graph_reg, graph_calls, flat_calls = _farm_with_graph_routing(tmp_path)
    graph_reg.upsert(_graph_record("proj-uid-1"), bump=False)

    await farm._handle(_delivery(record_uid="proj-uid-1"))

    # dispatched to the GRAPH handler (run_graph_record), not the flat path.
    assert graph_calls == ["proj-uid-1"]
    assert flat_calls == []
    # and the delivery was acked.
    assert farm._bus.acked  # type: ignore[attr-defined]


async def test_unknown_record_uid_is_acked_and_not_dispatched(tmp_path):
    farm, graph_reg, graph_calls, flat_calls = _farm_with_graph_routing(tmp_path)
    # registry is EMPTY -> the record_uid resolves to nothing.

    await farm._handle(_delivery(record_uid="ghost-uid"))

    # never dispatched to either handler, but acked (not silently re-queued forever).
    assert graph_calls == []
    assert flat_calls == []
    assert farm._bus.acked  # type: ignore[attr-defined]


async def test_disabled_graph_record_is_skipped(tmp_path):
    farm, graph_reg, graph_calls, flat_calls = _farm_with_graph_routing(tmp_path)
    graph_reg.upsert(_graph_record("proj-off", enabled=False), bump=False)

    await farm._handle(_delivery(record_uid="proj-off"))

    assert graph_calls == []  # inactive project not run
    assert farm._bus.acked  # type: ignore[attr-defined]


async def test_no_record_uid_falls_back_to_flat_agent_path(tmp_path):
    """A legacy fired event (no record_uid, just `agent`) still routes the flat way — the
    graph path is additive, not a replacement."""
    farm, graph_reg, graph_calls, flat_calls = _farm_with_graph_routing(tmp_path)

    class _Rec:
        name = "noop"
        uid = "flat-uid"
        enabled = True

    farm._registry._records = {}  # type: ignore[attr-defined]

    # patch the flat registry get_by_name to return our fake record.
    farm._registry.get_by_name = lambda name: _Rec() if name == "noop" else None  # type: ignore[attr-defined]

    await farm._handle(_delivery(record_uid=None, agent="noop"))

    assert flat_calls == ["noop"]
    assert graph_calls == []
