"""The delivery node — where a result leaves the runtime.

Channels:
  * ``whatsapp`` — connect to the bridge's ``/agent`` Socket.IO namespace with the
    agent's token, ``emit('sendMessage', {targetId, text})``, await the ack. Connect
    per-delivery (stateless, fits the transient-task model; one send/day for news).
  * ``bus`` — publish the result as an event onto a stream (a dashboard observes it).

The bridge auth/token is a **secret from config**, never from the DSL record. A
failed delivery raises loudly — a dropped message must never look like a success.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from agent_bus_client import new_event
from agent_bus_client.bus import BusClient

from ..config import Settings
from ..dsl import Delivery

log = logging.getLogger("agent_runtime.delivery")


class DeliveryError(Exception):
    """A delivery attempt failed (transport, auth, or a negative ack)."""


async def deliver(
    delivery: Delivery,
    text: str,
    *,
    settings: Settings,
    bus: BusClient | None = None,
    sio_factory: Callable[[], Any] | None = None,
    cid: str = "",
) -> str:
    """Send ``text`` via the record's channel. Returns a delivery id (messageId or
    stream entry id). Raises DeliveryError on failure."""
    channel = delivery.channel
    if channel == "whatsapp":
        return await _deliver_whatsapp(delivery.target, text, settings, sio_factory)
    if channel == "bus":
        return await _deliver_bus(delivery.target, text, settings, bus, cid)
    raise DeliveryError(f"unsupported delivery channel: {channel!r}")


async def deliver_file(
    path: str,
    text: str,
    *,
    mode: str = "overwrite",
    writer: Callable[[str, str, str], str] | None = None,
) -> str:
    """Write ``text`` to a file at ``path`` (§8 File Destination). ``mode`` is
    ``overwrite`` or ``append``. ``writer`` is an injectable IO seam (path, text, mode)
    -> id so tests mock the filesystem; the default performs the real write. Returns the
    path written. Raises DeliveryError on any IO failure (never a silent drop)."""
    if not path or not path.strip():
        raise DeliveryError("file destination: target path is empty")
    if mode not in ("overwrite", "append"):
        raise DeliveryError(f"file destination: unsupported mode {mode!r}")
    if writer is not None:
        return writer(path, text, mode)
    try:
        with open(path, "a" if mode == "append" else "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        raise DeliveryError(f"file destination: could not write {path}: {exc}") from exc
    log.info("delivered to file %s (mode=%s, %d chars)", path, mode, len(text))
    return path


async def deliver_web(
    url: str,
    text: str,
    *,
    method: str = "POST",
    caller: Callable[[str, str, str], Any] | None = None,
) -> str:
    """Call an outbound Web API at ``url`` with ``text`` as the body (§8 Web
    Destination). ``caller`` is an injectable IO seam (method, url, text) -> id so tests
    mock the HTTP call; the default performs the real request. Returns a delivery id
    (the response id/status). Raises DeliveryError on failure."""
    if not url or not url.strip():
        raise DeliveryError("web destination: target url is empty")
    if caller is not None:
        result = caller(method, url, text)
        return str(result if result is not None else "")
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - httpx is a runtime dep
        raise DeliveryError(
            "web destination: httpx is required for a live outbound call"
        ) from exc
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.request(method, url, content=text.encode("utf-8"))
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - surfaced loudly as DeliveryError
        raise DeliveryError(f"web destination: {method} {url} failed: {exc}") from exc
    log.info("delivered to web %s %s (status=%s)", method, url, resp.status_code)
    return str(resp.status_code)


async def _deliver_whatsapp(
    target_id: str,
    text: str,
    settings: Settings,
    sio_factory: Callable[[], Any] | None,
) -> str:
    if not settings.whatsapp_token:
        raise DeliveryError(
            "WHATSAPP_TOKEN is empty — refusing to attempt delivery (set it in the "
            "environment, never in the DSL)"
        )
    if sio_factory is None:
        import socketio  # local import so the package loads without socketio at rest

        sio_factory = socketio.AsyncClient

    sio = sio_factory()
    try:
        try:
            await sio.connect(
                settings.whatsapp_bridge_url,
                namespaces=["/agent"],
                auth={"agentName": settings.whatsapp_agent_name,
                      "token": settings.whatsapp_token},
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a loud DeliveryError
            raise DeliveryError(
                f"could not connect to whatsapp bridge {settings.whatsapp_bridge_url}: {exc}"
            ) from exc

        try:
            ack = await sio.call(
                "sendMessage",
                {"targetId": target_id, "text": text},
                namespace="/agent",
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001
            raise DeliveryError(f"sendMessage to {target_id} failed: {exc}") from exc

        if not isinstance(ack, dict) or not ack.get("ok"):
            raise DeliveryError(f"bridge rejected sendMessage to {target_id}: {ack!r}")
        message_id = ack.get("messageId", "")
        log.info("delivered to whatsapp %s (messageId=%s)", target_id, message_id)
        return message_id
    finally:
        try:
            await sio.disconnect()
        except Exception as exc:  # noqa: BLE001
            log.warning("error disconnecting from bridge: %s", exc)


async def _deliver_bus(
    target_stream_id: str,
    text: str,
    settings: Settings,
    bus: BusClient | None,
    cid: str,
) -> str:
    if bus is None:
        raise DeliveryError("bus delivery requested but no BusClient is available")
    stream = bus.stream_key(target_stream_id)
    env = new_event(
        stream_id=target_stream_id,
        cid=cid or target_stream_id,
        sid=1,
        sender=settings.sender_id,
        event_type="agent.result",
        data={"output": text},
    )
    entry_id = await bus.publish(stream, env)
    log.info("delivered to bus stream %s (entry=%s)", stream, entry_id)
    return entry_id
