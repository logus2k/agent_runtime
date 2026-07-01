"""The runner — the agent pipeline handler the farm dispatches to.

The agent runs as a **composer graph** executed by the ``GraphExecutor``: for a linear
record the graph is ``trigger → agent → destination``, walked by following real edges
(not a hardcoded stage order). Node handlers do the work:
    trigger  → build the task (input.template + vars + event overrides)
    agent    → brain (FC loop over agent_server + MCP) + guardrail (Proxy)
    dest     → delivery (whatsapp | bus | tts)
Run events are emitted to the bus keyed by the trigger's cid so the run is replayable in
the agent_bus console.

This is the same behaviour the flat pipeline had, but the execution path is now the new
graph structure — so a branching/looping agent (Branch/Loop nodes) runs through the very
same executor once its IR is produced.

Failures are loud: a guardrail block or a delivery error raises (the farm logs it and
emits no false success). Observability-emit failures are logged but don't fail the job
— the primary effect (delivery) already happened, and a dropped trace is not a dropped
message.
"""

from __future__ import annotations

import logging

from agent_bus_client import EventEnvelope, new_event
from agent_bus_client.bus import BusClient

from .agent_server_client import AgentServerClient
from .composer.executor import ExecContext, GraphExecutor
from .composer.ir import IREdge, IRGraph, IRNode
from .config import Settings
from .dsl import AgentRecord, Delivery
from .nodes.brain import run_brain
from .nodes.delivery import deliver
from .nodes.guardrail import apply_guardrails
from .mcp_client import MCPClient

log = logging.getLogger("agent_runtime.runner")


def ir_from_record(record: AgentRecord) -> IRGraph:
    """Build the graph-form IR for a linear agent record: trigger → agent →
    destination. The destination node's kind is its channel, so the runner registers a
    channel handler under that kind. This is the degenerate (linear) IR; a graph-form
    record (Branch/Loop) would carry its own nodes/edges."""
    dest_kind = record.delivery.channel
    nodes = {
        "trigger": IRNode("trigger", "trigger", {"agent": record.name}),
        "agent": IRNode("agent", "agent", {"persona": record.brain.persona}),
        dest_kind: IRNode(dest_kind, dest_kind, {"target": record.delivery.target}),
    }
    edges = [IREdge("trigger", "agent"), IREdge("agent", dest_kind)]
    return IRGraph(nodes=nodes, edges=edges, entry="trigger")


class Runner:
    def __init__(
        self,
        settings: Settings,
        bus: BusClient,
        *,
        agent_server: AgentServerClient | None = None,
        sio_factory=None,
    ):
        self._settings = settings
        self._bus = bus
        self._agent_server = agent_server or AgentServerClient(settings.agent_server_url)
        self._sio_factory = sio_factory

    async def run(self, record: AgentRecord, env: EventEnvelope) -> None:
        s = self._settings
        cid = env.header.cid
        # Every run event carries the agent's uid + (snapshot) name so the admin
        # runs view can group/label by agent without a registry lookup.
        lbl = {"agent_uid": record.uid, "agent_name": record.name}

        async def emit(event_type: str, data: dict) -> None:
            await self._emit(cid, event_type, {**lbl, **data})

        overrides = (env.payload.data or {}).get("vars") or {}
        task_text = self._build_task(record, overrides)

        mcp = self._make_mcp(record)

        async def on_tool(turn, name, args, result):
            await emit("tool.exec", {"turn": turn, "name": name, "args": args})
            await emit("tool.result", {"turn": turn, "name": name, "result": result[:2000]})

        # --- node handlers (the work); the executor does the routing ---
        async def h_trigger(node, value, ctx):
            return task_text  # the task flows into the agent

        async def h_agent(node, value, ctx):
            brain_res = await run_brain(
                record, value, agent_server=self._agent_server, mcp=mcp, on_tool=on_tool
            )
            if brain_res.thought:
                await emit("agent.thought", {"thought": brain_res.thought})
            if not brain_res.answer.strip():
                await emit("workflow.terminated", {"reason": "empty_answer"})
                raise RuntimeError(f"agent '{record.name}' produced an empty answer (cid={cid})")
            gr = apply_guardrails(record.guardrails, brain_res.answer)
            if not gr.ok:
                log.error("guardrail blocked agent '%s' (cid=%s): %s", record.name, cid, gr.reason)
                await emit("workflow.terminated", {"reason": "guardrail_blocked", "detail": gr.reason})
                raise RuntimeError(f"guardrail blocked delivery for '{record.name}': {gr.reason}")
            ctx.scratch["turns_used"] = brain_res.turns_used
            return brain_res.answer

        async def h_deliver(node, value, ctx):
            delivery_id = await deliver(
                record.delivery, value, settings=s, bus=self._bus,
                sio_factory=self._sio_factory, cid=cid,
            )
            await emit(
                "agent.result",
                {"output": value[:4000], "delivery_id": delivery_id,
                 "channel": record.delivery.channel},
            )
            return delivery_id

        # Execute the agent as a graph: trigger → agent → destination.
        graph = ir_from_record(record)
        handlers = {"trigger": h_trigger, "agent": h_agent, record.delivery.channel: h_deliver}
        exec_ctx = ExecContext(cid=cid, sender=self._settings.sender_id)
        await GraphExecutor(handlers).run(graph, None, exec_ctx)

        await emit(
            "workflow.terminated",
            {"reason": "done", "turns": exec_ctx.scratch.get("turns_used", 0)},
        )

    async def run_workflow(self, ir: IRGraph, env: EventEnvelope) -> None:
        """Execute a **multi-agent workflow** (graph-form IR) through the GraphExecutor.

        Unlike ``run`` (single closed-over ``record``), each node's config is
        self-contained: an ``agent`` node carries its own ``AgentRecord`` under
        ``config["record"]``, so N agents chain edge-to-edge — agent-1's answer becomes
        the incoming ``value`` (and thus the task) of agent-2. The executor's ``on_trace``
        hook emits an ``edge.traversed`` run event per edge crossed (the Edge/envelope
        contract made load-bearing at runtime). Tool-less agents only in this slice
        (personas), so no per-node MCP client is constructed."""
        s = self._settings
        cid = env.header.cid
        overrides = (env.payload.data or {}).get("vars") or {}
        initial_task = str((env.payload.data or {}).get("task") or "")

        async def emit_for(record: AgentRecord | None, event_type: str, data: dict) -> None:
            lbl = (
                {"agent_uid": record.uid, "agent_name": record.name}
                if record is not None else {}
            )
            await self._emit(cid, event_type, {**lbl, **data})

        async def h_trigger(node, value, ctx):
            # The workflow's seed: an optional task carried on the trigger event. The
            # first agent applies its own input template to this value.
            return initial_task

        async def h_agent(node, value, ctx):
            record = self._record_from_node(node)
            if record.tools and record.tools.allow:
                # First slice is tool-less by design (no per-node MCP client). Fail loud
                # rather than silently ignore a declared tool.
                raise RuntimeError(
                    f"workflow agent '{record.name}' declares tools, but the multi-agent "
                    f"slice is tool-less (personas only) — per-node MCP not wired yet"
                )
            task = self._build_agent_task(record, value, overrides)
            brain_res = await run_brain(
                record, task, agent_server=self._agent_server, mcp=None
            )
            if brain_res.thought:
                await emit_for(record, "agent.thought", {"thought": brain_res.thought})
            if not brain_res.answer.strip():
                await emit_for(record, "workflow.terminated", {"reason": "empty_answer"})
                raise RuntimeError(
                    f"agent '{record.name}' produced an empty answer (cid={cid})"
                )
            gr = apply_guardrails(record.guardrails, brain_res.answer)
            if not gr.ok:
                log.error(
                    "guardrail blocked agent '%s' (cid=%s): %s",
                    record.name, cid, gr.reason,
                )
                await emit_for(
                    record, "workflow.terminated",
                    {"reason": "guardrail_blocked", "detail": gr.reason},
                )
                raise RuntimeError(
                    f"guardrail blocked delivery for '{record.name}': {gr.reason}"
                )
            ctx.scratch["turns_used"] = (
                ctx.scratch.get("turns_used", 0) + brain_res.turns_used
            )
            await emit_for(record, "agent.result", {"output": brain_res.answer[:4000]})
            return brain_res.answer

        async def h_deliver(node, value, ctx):
            delivery = Delivery(
                channel=node.kind,
                target=str(node.config.get("target") or ""),
                target_name=str(node.config.get("target_name") or ""),
            )
            delivery_id = await deliver(
                delivery, value, settings=s, bus=self._bus,
                sio_factory=self._sio_factory, cid=cid,
            )
            await emit_for(
                None, "agent.result",
                {"output": value[:4000], "delivery_id": delivery_id,
                 "channel": delivery.channel},
            )
            return delivery_id

        def on_trace(src: str, dst: str, port: str, ctx) -> None:
            # The executor's trace hook is SYNC, so buffer each edge here and emit them
            # (async) after the walk — deterministic, no fire-and-forget task races.
            ctx.scratch.setdefault("edges", []).append((src, dst, port))

        # Register a handler for every destination channel present in the graph.
        handlers: dict = {"trigger": h_trigger, "agent": h_agent}
        for n in ir.nodes.values():
            if n.kind not in ("trigger", "agent"):
                handlers[n.kind] = h_deliver

        exec_ctx = ExecContext(cid=cid, sender=self._settings.sender_id)
        await GraphExecutor(handlers, on_trace=on_trace).run(ir, None, exec_ctx)

        for src, dst, port in exec_ctx.scratch.get("edges", []):
            await self._emit(cid, "edge.traversed", {"src": src, "dst": dst, "port": port})

        await self._emit(
            cid, "workflow.terminated",
            {"reason": "done", "turns": exec_ctx.scratch.get("turns_used", 0)},
        )

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _record_from_node(node: IRNode) -> AgentRecord:
        """Build the per-node AgentRecord from an agent IRNode's embedded config."""
        rec = node.config.get("record")
        if rec is None:
            raise RuntimeError(f"agent node '{node.id}' has no embedded record config")
        return AgentRecord.model_validate(rec)

    def _build_agent_task(
        self, record: AgentRecord, incoming: object, overrides: dict
    ) -> str:
        """The task for one agent in a workflow. If the agent has an input template, it
        is formatted with its vars + event overrides + the incoming value bound to
        ``{input}`` (so a template can weave the upstream answer in). If there is no
        template, the incoming value flows through verbatim — this is how agent-1's
        answer becomes agent-2's task edge-to-edge."""
        template = record.input.template
        incoming_text = "" if incoming is None else str(incoming)
        if not template:
            return incoming_text
        merged = {**record.input.vars, "input": incoming_text, **overrides}
        try:
            return template.format(**merged)
        except KeyError as exc:
            raise RuntimeError(
                f"agent '{record.name}' input.template references missing var {exc}"
            ) from exc

    def _build_task(self, record: AgentRecord, overrides: dict) -> str:
        template = record.input.template
        if not template:
            return ""
        merged = {**record.input.vars, **overrides}
        try:
            return template.format(**merged)
        except KeyError as exc:
            raise RuntimeError(
                f"agent '{record.name}' input.template references missing var {exc}"
            ) from exc

    def _make_mcp(self, record: AgentRecord) -> MCPClient | None:
        if not (record.tools and record.tools.allow):
            return None
        if record.tools.server != self._settings.mcp_server_key:
            raise RuntimeError(
                f"agent '{record.name}' uses MCP server '{record.tools.server}' but the "
                f"runtime is configured for '{self._settings.mcp_server_key}'"
            )
        return MCPClient(self._settings.mcp_url, server=record.tools.server)

    async def _emit(self, cid: str, event_type: str, data: dict) -> None:
        """Emit one run event. Logged-but-not-fatal on failure (a dropped trace is not
        a dropped message)."""
        try:
            sid = await self._bus.incr(f"sid:{cid}")
            await self._bus.expire(f"sid:{cid}", self._settings.sid_ttl_s)
            env = new_event(
                stream_id=self._settings.runs_stream_id,
                cid=cid,
                sid=sid,
                sender=self._settings.sender_id,
                event_type=event_type,
                data=data,
            )
            await self._bus.publish(
                self._bus.stream_key(self._settings.runs_stream_id), env
            )
        except Exception as exc:  # noqa: BLE001 - surfaced loudly, but never fails the job
            log.error("failed to emit run event %s (cid=%s): %s", event_type, cid, exc)
