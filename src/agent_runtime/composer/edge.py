"""Edge — a first-class, traced connection between two ports.

An Edge is NOT just a line on a canvas: it is the message-passing contract. To send a
message into an edge the sender must supply the edge's required header fields, and the
receiving block's input must comply. That header IS the agent_bus envelope header
(VERIFIED 2026-06-30): ``cid, sid, sender, timestamp`` — so every message is

    header (management/trace — universal) + payload (functional — typed per port).

We therefore reuse ``agent_bus_client``'s ``EventEnvelope`` rather than minting a 4th
copy of the envelope (agent_bus → agent_scheduler → agent_runtime already have it).
The runtime already passes these envelopes (``runner.py`` → ``new_event(...)``); the
Edge just makes the contract explicit at compose time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from agent_bus_client import EventEnvelope, new_event

from .schema import Port

# The header fields an Edge requires of any sender. These are exactly the agent_bus
# envelope header's identity/trace fields — the "management" half of every message.
REQUIRED_HEADER: tuple[str, ...] = ("cid", "sid", "sender", "timestamp")


@dataclass(frozen=True)
class TraceRecord:
    """The traversal trace an Edge publishes: who sent what, when, correlated by cid.

    A lightweight, serializable view over the envelope header — what a debug/trace UI
    renders per edge. ``timestamp`` is the envelope's UTC ISO-8601 string.
    """

    edge: str          # "<src_block>.<src_port> -> <dst_block>.<dst_port>"
    cid: str
    sid: int
    sender: str
    timestamp: str
    event_type: str

    def to_json(self) -> dict[str, Any]:
        return {
            "edge": self.edge,
            "cid": self.cid,
            "sid": self.sid,
            "sender": self.sender,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
        }


class Edge:
    """A typed, traceable wire from one block's ``out`` port to another's ``in`` port.

    Identity of the endpoints is by block uid + port; the payload's *shape* is the
    source port's schema, which must be compatible with the destination port's schema
    (that check is ``validate()``). The header (cid/sid/sender/UTC-ts) is stamped on
    every message via ``stamp()``.
    """

    def __init__(
        self,
        *,
        src_block: str,
        src_port: Port,
        dst_block: str,
        dst_port: Port,
        stream_id: str = "agent-runtime",
        event_type: str = "edge.traversed",
    ) -> None:
        if src_port.direction != "out":
            raise ValueError(f"edge source port '{src_port.name}' must be an 'out' port")
        if dst_port.direction != "in":
            raise ValueError(f"edge destination port '{dst_port.name}' must be an 'in' port")
        self.src_block = src_block
        self.src_port = src_port
        self.dst_block = dst_block
        self.dst_port = dst_port
        self.stream_id = stream_id
        self.event_type = event_type

    @property
    def label(self) -> str:
        return f"{self.src_block}.{self.src_port.name} -> {self.dst_block}.{self.dst_port.name}"

    @staticmethod
    def required_header() -> tuple[str, ...]:
        """The header fields a sender MUST provide to put a message on any edge."""
        return REQUIRED_HEADER

    def validate(self, src: Optional[Port] = None, dst: Optional[Port] = None) -> list[str]:
        """Is the source payload shape compatible with what the destination accepts?

        Returns a list of human-aimed error strings (empty == ok). Defaults to the
        edge's own endpoints; the explicit args exist so a graph validator can reuse
        the rule with resolved ports.
        """
        s = (src or self.src_port).schema
        d = (dst or self.dst_port).schema
        if not s.is_compatible_with(d):
            return [
                f"edge {self.label}: source schema {s.to_json()} is not compatible "
                f"with destination schema {d.to_json()}"
            ]
        return []

    def stamp(
        self,
        payload: Any,
        *,
        cid: str,
        sid: int,
        sender: str,
        timestamp: Optional[str] = None,
    ) -> EventEnvelope:
        """Build the traced message for this edge: the universal header + the typed
        payload. Delegates to ``agent_bus_client.new_event`` so the header is byte-for-
        byte the same envelope the bus/runtime already use.
        """
        data: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {"value": payload}
        return new_event(
            stream_id=self.stream_id,
            cid=cid,
            sid=sid,
            sender=sender,
            event_type=self.event_type,
            data=data,
            timestamp=timestamp,
        )

    def trace(self, env: EventEnvelope) -> TraceRecord:
        """Project an envelope crossing this edge into a TraceRecord (for debug/trace)."""
        h = env.header
        return TraceRecord(
            edge=self.label,
            cid=h.cid,
            sid=h.sid,
            sender=h.sender,
            timestamp=h.timestamp,
            event_type=h.event_type,
        )
