"""Step-by-step debugging — the *control* half of the Trace panel (documents/debug_specification.md).

A **DebugSession** pauses a single graph run BEFORE each node so the user can advance it one node
at a time, inspect the payload, continue, or stop. It is keyed by the run's ``cid`` (runs are
already cid-isolated). The executor awaits ``session.gate(...)`` before running each node; the admin
``/step`` · ``/continue`` · ``/stop`` endpoints drive the session from HTTP.

A process-global **DebugRegistry** (``registry``) maps ``cid -> DebugSession`` — the Runner creates
one when a run is fired with ``debug: true`` and removes it when the run ends; the endpoints look it
up. This mirrors the in-process ``events.hub`` singleton pattern (single farm process).

Inert when unused: no session means the executor never constructs or awaits a gate, so non-debug
runs are byte-for-byte unchanged.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

# Emit callback the executor hands to ``gate`` so this module stays free of admin/event imports —
# the Runner supplies a closure that stamps record_uid + previews the payload and publishes.
PauseEmit = Callable[[str, dict], Awaitable[None]]


class DebugStopped(Exception):
    """Raised inside the executor when a debug run is asked to stop — a clean abort (the Runner
    catches it and emits ``workflow.terminated {reason: "debug-stopped"}``)."""

    def __init__(self, cid: str) -> None:
        super().__init__(f"debug run {cid} stopped")
        self.cid = cid


class DebugSession:
    """One paused/stepping run. ``mode`` is ``'step'`` (pause before each node), ``'continue'``
    (run to completion, no more pausing), or ``'stopped'`` (abort at the next gate). A step token
    queue releases the gate: ``step()`` releases exactly one node; ``cont()``/``stop()`` set the
    mode and release any current wait so it re-reads the mode."""

    def __init__(self, cid: str, uid: str, owner: Optional[str] = None) -> None:
        self.cid = cid
        self.uid = uid                 # deployed record uid — for owner-gating the endpoints
        self.owner = owner             # record owner (sub) captured at creation
        self.mode = "step"             # 'step' | 'continue' | 'stopped'
        self.paused_node: Optional[str] = None
        self.started_at = time.time()
        self._tokens: "asyncio.Queue[int]" = asyncio.Queue()
        self._last_activity = time.monotonic()

    # --- lifecycle bookkeeping ---
    def touch(self) -> None:
        self._last_activity = time.monotonic()

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_activity

    def state(self) -> dict:
        return {"cid": self.cid, "uid": self.uid, "mode": self.mode,
                "paused_node": self.paused_node, "idle_seconds": round(self.idle_seconds, 1)}

    # --- the pause-gate (called by the executor before each node) ---
    async def gate(self, node_id: str, kind: str, incoming: Any,
                   emit: Optional[PauseEmit] = None) -> None:
        """Block before running ``node_id`` until the user steps/continues (or raise on stop).
        In 'continue' mode returns at once; in 'stopped' mode raises ``DebugStopped``."""
        self.touch()
        if self.mode == "stopped":
            raise DebugStopped(self.cid)
        if self.mode == "continue":
            return
        # step mode → announce the pause point, then wait for a token.
        self.paused_node = node_id
        if emit is not None:
            await emit("node.paused", {"node": node_id, "kind": kind, "incoming": incoming})
        await self._tokens.get()
        self.touch()
        self.paused_node = None
        if self.mode == "stopped":
            raise DebugStopped(self.cid)

    # --- control (called by the endpoints) ---
    def step(self) -> None:
        """Advance exactly one node."""
        self.touch()
        self._tokens.put_nowait(1)

    def cont(self) -> None:
        """Stop pausing; let the run finish normally."""
        self.touch()
        self.mode = "continue"
        self._tokens.put_nowait(1)     # release a current wait

    def stop(self) -> None:
        """Abort the run at the next gate."""
        self.touch()
        self.mode = "stopped"
        self._tokens.put_nowait(1)     # release a current wait so it can raise


class DebugRegistry:
    """Process-global map of active debug sessions, ``cid -> DebugSession``."""

    def __init__(self) -> None:
        self._sessions: dict[str, DebugSession] = {}

    def create(self, cid: str, uid: str, owner: Optional[str] = None) -> DebugSession:
        s = DebugSession(cid, uid, owner)
        self._sessions[cid] = s
        return s

    def get(self, cid: str) -> Optional[DebugSession]:
        return self._sessions.get(cid)

    def remove(self, cid: str) -> None:
        self._sessions.pop(cid, None)

    def all(self) -> list[DebugSession]:
        return list(self._sessions.values())


# Process-global singleton (the Runner, the admin endpoints, and the idle reaper share it).
registry = DebugRegistry()
