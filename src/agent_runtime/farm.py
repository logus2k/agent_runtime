"""The farm — the bus consumer + dispatch loop.

One lean async process consumes trigger events off the shared farm stream
(``stream:agent-runtime``) as a consumer group, resolves each event's
``{ agent: <id> }`` to a record, and dispatches a **bounded transient task** that
runs the agent pipeline. Idle agents cost nothing; a trigger spins up a task that
runs and exits.

Guarantees (per documents/technical_architecture.md §3):
  * **Bounded concurrency** — a semaphore caps in-flight jobs (~ agent_server slots).
  * **Bounded jobs** — each carries a timeout; a hung/failed job can't take down the
    loop or its neighbours.
  * **At-least-once + idempotency** — the bus may redeliver after a reclaim; we dedupe
    on ``cid``+``sid`` before doing work, and the reaper reclaims abandoned jobs.

No silent failures: every job failure is logged loudly and (when wired) surfaced to
the bus as a terminal event. The dispatch path never swallows an exception.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from agent_bus_client import EventEnvelope
from agent_bus_client.bus import BusClient, Delivery, make_consumer

from .config import Settings
from .registry import Registry

log = logging.getLogger("agent_runtime.farm")

# A handler runs one resolved agent for one trigger event. The farm owns
# bus/dispatch/idempotency/ack; the handler owns the pipeline (brain→…→delivery).
AgentHandler = Callable[["AgentRecordRef", EventEnvelope], Awaitable[None]]

# Imported lazily for the type alias above without a hard cycle.
from .dsl import AgentRecord as AgentRecordRef  # noqa: E402


class Farm:
    def __init__(
        self,
        settings: Settings,
        registry: Registry,
        handler: AgentHandler | None = None,
        *,
        bus: BusClient | None = None,
    ):
        self._settings = settings
        self._registry = registry
        self._handler = handler
        self._bus = bus
        self._consumer = make_consumer(settings.consumer_name)
        self._sem = asyncio.Semaphore(settings.max_concurrency)
        self._jobs: set[asyncio.Task] = set()
        self._running = False
        self._tasks: list[asyncio.Task] = []

    @property
    def bus(self) -> BusClient:
        if self._bus is None:
            raise RuntimeError("farm bus not connected")
        return self._bus

    def set_handler(self, handler: AgentHandler) -> None:
        """Attach the pipeline handler (built after connect so it can use the bus)."""
        self._handler = handler

    # --- lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        """Connect to Valkey with bounded retry (services come up independently),
        then ensure the consumer group exists. Loud on every attempt."""
        if self._bus is not None:
            return
        s = self._settings
        last_exc: Exception | None = None
        for attempt in range(1, s.connect_retries + 1):
            try:
                self._bus = await BusClient.create(
                    s.valkey_host,
                    s.valkey_port,
                    stream_prefix=s.stream_prefix,
                    dlq_stream=s.dlq_stream,
                    stream_ttl_s=s.stream_ttl_s,
                )
                log.info(
                    "connected to valkey %s:%s (attempt %d)",
                    s.valkey_host, s.valkey_port, attempt,
                )
                break
            except Exception as exc:  # noqa: BLE001 - surfaced via log + re-raise below
                last_exc = exc
                log.warning(
                    "valkey connect attempt %d/%d failed: %s",
                    attempt, s.connect_retries, exc,
                )
                await asyncio.sleep(s.connect_retry_delay_s)
        if self._bus is None:
            raise RuntimeError(
                f"could not connect to valkey {s.valkey_host}:{s.valkey_port} "
                f"after {s.connect_retries} attempts: {last_exc}"
            )
        await self._bus.ensure_group(
            s.farm_stream_key(), s.consumer_group, start="$"
        )
        log.info(
            "consumer group '%s' ready on %s as '%s'",
            s.consumer_group, s.farm_stream_key(), self._consumer,
        )

    async def start(self) -> None:
        """Start the consume + reaper loops as background tasks."""
        if self._handler is None:
            raise RuntimeError("farm has no handler; call set_handler() before start()")
        await self.connect()
        self._running = True
        self._tasks = [
            asyncio.create_task(self._consume_loop(), name="farm-consume"),
            asyncio.create_task(self._reaper_loop(), name="farm-reaper"),
        ]
        log.info("farm started (max_concurrency=%d)", self._settings.max_concurrency)

    async def stop(self) -> None:
        """Stop the loops and let in-flight jobs finish (bounded by their timeout)."""
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        if self._jobs:
            log.info("waiting for %d in-flight job(s) to finish", len(self._jobs))
            await asyncio.gather(*self._jobs, return_exceptions=True)
        if self._bus is not None:
            await self._bus.close()
        log.info("farm stopped")

    # --- loops --------------------------------------------------------------

    async def _consume_loop(self) -> None:
        s = self._settings
        stream = s.farm_stream_key()
        poll_s = s.poll_ms / 1000.0
        while self._running:
            try:
                # Non-blocking read: a BLOCK would monopolize glide's single
                # multiplexed connection and starve dispatched jobs' commands.
                deliveries = await self.bus.read_group(
                    [stream],
                    s.consumer_group,
                    self._consumer,
                    count=s.read_count,
                    block_ms=None,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - loop must survive transient bus errors, loudly
                log.error("read_group error (continuing): %s", exc, exc_info=True)
                await asyncio.sleep(1.0)
                continue
            for d in deliveries:
                self._spawn(d)
            await asyncio.sleep(poll_s)

    async def _reaper_loop(self) -> None:
        """Reclaim entries abandoned by a crashed/slow consumer (XAUTOCLAIM)."""
        s = self._settings
        stream = s.farm_stream_key()
        while self._running:
            try:
                await asyncio.sleep(s.reaper_interval_s)
                cursor = "0-0"
                while True:
                    cursor, claimed = await self.bus.reclaim(
                        stream,
                        s.consumer_group,
                        self._consumer,
                        s.reaper_min_idle_ms,
                        start=cursor,
                        count=s.read_count,
                    )
                    for d in claimed:
                        log.info("reclaimed abandoned entry %s", d.entry_id)
                        self._spawn(d)
                    if cursor == "0-0" or not claimed:
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reaper must survive, loudly
                log.error("reaper error (continuing): %s", exc, exc_info=True)

    # --- dispatch -----------------------------------------------------------

    def _spawn(self, delivery: Delivery) -> None:
        task = asyncio.create_task(self._handle(delivery))
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    async def _handle(self, delivery: Delivery) -> None:
        """Process one delivery end-to-end: dedupe → run (bounded) → ack.

        Always acks after handling (success or handled failure) so a logic-poison
        message is not redelivered forever; the reaper covers genuine crashes
        (process died before ack)."""
        s = self._settings
        env = delivery.envelope
        cid, sid = env.header.cid, env.header.sid
        data = env.payload.data or {}
        # Route by the immutable uid (agent_uid). Fall back to the friendly name
        # (legacy `agent`, or `agent_name`) so jobs minted before the uid migration
        # still resolve. `ref` is just for logs.
        agent_uid = data.get("agent_uid")
        agent_name = data.get("agent_name") or data.get("agent")
        ref = agent_uid or agent_name

        try:
            if not agent_uid and not agent_name:
                log.error(
                    "trigger %s has no 'agent_uid'/'agent' in payload.data (%s) — acking, "
                    "cannot route", delivery.entry_id, data,
                )
                return  # ack in finally

            # Idempotency: first INCR wins; a redelivery sees n>1 and is skipped.
            dedupe_key = f"ar:dedupe:{cid}:{sid}"
            n = await self.bus.incr(dedupe_key)
            if n == 1:
                await self.bus.expire(dedupe_key, s.dedupe_ttl_s)
            else:
                log.info(
                    "duplicate trigger cid=%s sid=%s (agent=%s) — already processed, "
                    "skipping", cid, sid, ref,
                )
                return  # ack in finally

            record = (
                self._registry.get(agent_uid) if agent_uid
                else self._registry.get_by_name(agent_name)
            )
            if record is None:
                log.error(
                    "trigger %s names unknown agent (uid=%s name=%s; known uids: %s) — "
                    "acking", delivery.entry_id, agent_uid, agent_name, self._registry.ids,
                )
                return  # ack in finally

            if not record.enabled:
                log.info(
                    "agent '%s' (%s) is inactive — skipping (cid=%s sid=%s)",
                    record.name, record.uid, cid, sid,
                )
                return  # ack in finally

            async with self._sem:
                log.info(
                    "running agent '%s' (%s) (cid=%s sid=%s)",
                    record.name, record.uid, cid, sid,
                )
                await asyncio.wait_for(
                    self._handler(record, env), timeout=s.job_timeout_s
                )
                log.info("agent '%s' completed (cid=%s)", record.name, cid)

        except asyncio.TimeoutError:
            log.error(
                "agent '%s' timed out after %ds (cid=%s)",
                ref, s.job_timeout_s, cid,
            )
        except asyncio.CancelledError:
            log.warning("agent '%s' cancelled (cid=%s)", ref, cid)
            raise
        except Exception as exc:  # noqa: BLE001 - one job's failure must not kill the farm
            log.error(
                "agent '%s' failed (cid=%s): %s", ref, cid, exc, exc_info=True
            )
        finally:
            try:
                await self.bus.ack(
                    delivery.stream, s.consumer_group, [delivery.entry_id]
                )
            except Exception as exc:  # noqa: BLE001
                log.error("ack failed for %s: %s", delivery.entry_id, exc)
