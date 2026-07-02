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
from .dsl import AgentRecord, Brain, Delivery, Guardrails, Judge, Rag
from .dsl_graph import GraphNode, GraphRecord, from_flat_record
from .graph_executor import GraphWorkflowExecutor, WalkContext
from .nodes.brain import run_brain
from .nodes.delivery import deliver
from .nodes.guardrail import apply_guardrails
from .nodes.loop import run_agent_loop
from .nodes.rag import retrieve_and_inject
from .mcp_client import MCPClient
from .skills.registry import SkillRegistry, get_registry

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
        # The skill registry (§8.3) — loaded once from settings.skills_dir and shared
        # across runs. Failures to load degrade to None (skills simply don't inject).
        self._skills: SkillRegistry | None = None
        try:
            self._skills = get_registry(settings.skills_dir)
        except Exception as exc:  # noqa: BLE001 - surface loudly, never block the runner
            log.error("skill registry failed to load from %s: %s", settings.skills_dir, exc)

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
                record, value, agent_server=self._agent_server, mcp=mcp,
                on_tool=on_tool, skills=self._skills,
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
                record, task, agent_server=self._agent_server, mcp=None,
                skills=self._skills,
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

    async def run_graph_record(self, record: GraphRecord, env: EventEnvelope) -> None:
        """Execute a **persisted graph/workflow record** (``GraphRecord``) through the
        ``GraphWorkflowExecutor`` — the graph form of §9.3 with fan-out / per-message
        fan-in (§9.3.2).

        The decomposed node kinds (§8.1) each get a handler:
            initiator   → seed the workflow with the trigger's task
            rag         → pre-inference retrieve-then-inject (stub inject for now)
            agent       → brain (FC loop over agent_server + MCP)
            guardrail   → output-side check; raises loudly on a block
            destination → delivery (whatsapp | bus | tts)

        A legacy flat record is run by passing ``from_flat_record(flat)`` here — same
        handlers, so the News Agent runs unchanged via the shim."""
        s = self._settings
        cid = env.header.cid
        overrides = (env.payload.data or {}).get("vars") or {}
        initial_task = str((env.payload.data or {}).get("task") or "")

        async def emit_for(node_record: AgentRecord | None, event_type: str, data: dict) -> None:
            lbl = (
                {"agent_uid": node_record.uid, "agent_name": node_record.name}
                if node_record is not None else {"agent_uid": record.uid, "agent_name": record.name}
            )
            await self._emit(cid, event_type, {**lbl, **data})

        async def h_initiator(node: GraphNode, value, ctx: WalkContext):
            # The workflow's seed: an optional task carried on the trigger event. The
            # first agent applies its own input template to this value.
            return initial_task

        async def h_rag(node: GraphNode, value, ctx: WalkContext):
            # RAG-pre (§8.1): retrieve-then-inject BEFORE the agent. Retrieve context for
            # the incoming task and inject it into the value flowing to the downstream
            # agent. Degrades gracefully — a down retrieval backend passes through (loud
            # warning), never crashes the run. Loud if the config itself is malformed.
            rag = Rag.model_validate(node.config.get("rag") or {})
            injected = await retrieve_and_inject(rag, value, settings=s)
            grew = len(str(injected)) > len(str(value or ""))
            await self._emit(
                cid, "rag.retrieved",
                {"node": node.id, "domains": rag.domains, "injected": grew},
            )
            return injected

        async def h_agent(node: GraphNode, value, ctx: WalkContext):
            node_record = self._graph_agent_record(node)
            mcp = self._make_mcp(node_record)
            task = self._build_agent_task(node_record, value, overrides)

            # One whole agent invocation (the unit the OUTER loop repeats). Distinct from
            # the Brain node's INNER tools.max_rounds loop, which lives inside run_brain.
            async def run_once(loop_task: str) -> str:
                brain_res = await run_brain(
                    node_record, loop_task, agent_server=self._agent_server, mcp=mcp,
                    skills=self._skills,
                )
                if brain_res.thought:
                    await emit_for(node_record, "agent.thought", {"thought": brain_res.thought})
                if not brain_res.answer.strip():
                    await emit_for(node_record, "workflow.terminated", {"reason": "empty_answer"})
                    raise RuntimeError(
                        f"agent '{node_record.name}' produced an empty answer (cid={cid})"
                    )
                ctx.scratch["turns_used"] = (
                    ctx.scratch.get("turns_used", 0) + brain_res.turns_used
                )
                return brain_res.answer

            # One Judge invocation (only used by the 'judge' loop type). A Judge is just an
            # agent profile (its persona + sampling) run in the judging role over the
            # outcome; its raw text is read as a verdict by run_agent_loop.
            async def run_judge(judge: Judge, judge_task: str) -> str:
                judge_record = self._judge_record(node_record, judge)
                jres = await run_brain(
                    judge_record, judge_task, agent_server=self._agent_server, mcp=None,
                    skills=self._skills,
                )
                await emit_for(
                    node_record, "loop.verdict",
                    {"judge": judge.persona, "output": jres.answer[:2000]},
                )
                return jres.answer

            loop_res = await run_agent_loop(
                node_record.loop, task, run_once, run_judge=run_judge
            )
            await emit_for(
                node_record, "loop.done",
                {"iterations": loop_res.iterations, "stopped_by": loop_res.stopped_by},
            )
            await emit_for(node_record, "agent.result", {"output": loop_res.outcome[:4000]})
            return loop_res.outcome

        async def h_guardrail(node: GraphNode, value, ctx: WalkContext):
            guardrails = Guardrails.model_validate(node.config.get("guardrails") or {})
            gr = apply_guardrails(guardrails, str(value))
            if not gr.ok:
                log.error("guardrail node '%s' blocked (cid=%s): %s", node.id, cid, gr.reason)
                await self._emit(
                    cid, "workflow.terminated",
                    {"reason": "guardrail_blocked", "detail": gr.reason, "node": node.id},
                )
                raise RuntimeError(
                    f"guardrail node '{node.id}' blocked delivery: {gr.reason}"
                )
            return value

        async def h_destination(node: GraphNode, value, ctx: WalkContext):
            delivery = Delivery(
                channel=node.config.get("channel") or "bus",
                target=str(node.config.get("target") or ""),
                target_name=str(node.config.get("target_name") or ""),
            )
            delivery_id = await deliver(
                delivery, value, settings=s, bus=self._bus,
                sio_factory=self._sio_factory, cid=cid,
            )
            await self._emit(
                cid, "agent.result",
                {"agent_uid": record.uid, "agent_name": record.name,
                 "output": str(value)[:4000], "delivery_id": delivery_id,
                 "channel": delivery.channel},
            )
            return delivery_id

        def on_trace(src: str, dst: str, port: str, ctx: WalkContext) -> None:
            ctx.scratch.setdefault("edges", []).append((src, dst, port))

        handlers = {
            "initiator": h_initiator,
            "rag": h_rag,
            "agent": h_agent,
            "guardrail": h_guardrail,
            "destination": h_destination,
        }
        walk_ctx = WalkContext(cid=cid, sender=self._settings.sender_id)
        await GraphWorkflowExecutor(handlers, on_trace=on_trace).run(record, None, walk_ctx)

        for src, dst, port in walk_ctx.scratch.get("edges", []):
            await self._emit(cid, "edge.traversed", {"src": src, "dst": dst, "port": port})

        await self._emit(
            cid, "workflow.terminated",
            {"reason": "done", "turns": walk_ctx.scratch.get("turns_used", 0),
             "agent_uid": record.uid, "agent_name": record.name},
        )

    def run_flat_via_shim(self, record: AgentRecord, env: EventEnvelope):
        """Compat entry (§9.3): run a legacy flat ``AgentRecord`` by lifting it to a
        degenerate ``GraphRecord`` (initiator → [rag] → agent → [guardrail] →
        destination) and executing it through the graph path — so the News Agent runs
        unchanged through the SAME graph executor as native workflows."""
        return self.run_graph_record(from_flat_record(record), env)

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _graph_agent_record(node: GraphNode) -> AgentRecord:
        """Build the per-node ``AgentRecord`` from a graph agent node's embedded config.
        tools/skills stay on the agent (they are in this record); rag/guardrails are
        their own nodes and were stripped from the embed by the shim."""
        rec = node.config.get("record")
        if rec is None:
            raise RuntimeError(f"agent node '{node.id}' has no embedded record config")
        return AgentRecord.model_validate(rec)

    @staticmethod
    def _judge_record(agent_record: AgentRecord, judge: Judge) -> AgentRecord:
        """A minimal ``AgentRecord`` that runs the embedded Judge as a plain agent
        (persona + sampling only — no tools/skills/loop of its own). Reuses the host
        agent's identity/version so run events stay attributable; the Brain node only
        needs a valid record to POST the Judge's persona to agent_server."""
        return AgentRecord(
            version=agent_record.version,
            uid=agent_record.uid,
            name=f"{agent_record.name}::judge",
            brain=Brain(persona=judge.persona, llm=judge.llm),
            delivery=agent_record.delivery,
        )

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
