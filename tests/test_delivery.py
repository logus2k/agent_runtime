"""Step 4: delivery node — whatsapp (faked socket) + bus channels. No live send."""

import dataclasses

import pytest

from agent_runtime.config import Settings
from agent_runtime.dsl import Delivery
from agent_runtime.nodes.delivery import DeliveryError, deliver


class FakeSio:
    def __init__(self, ack):
        self._ack = ack
        self.connected = False
        self.disconnected = False
        self.auth = None
        self.sent = None

    async def connect(self, url, namespaces=None, auth=None):
        self.connected = True
        self.url = url
        self.namespaces = namespaces
        self.auth = auth

    async def call(self, event, data, namespace=None, timeout=None):
        self.sent = {"event": event, "data": data, "namespace": namespace}
        return self._ack

    async def disconnect(self):
        self.disconnected = True


class FakeBus:
    def __init__(self):
        self.published = []

    def stream_key(self, sid):
        return f"stream:{sid}"

    async def publish(self, stream, env):
        self.published.append((stream, env))
        return "1-0"


def _settings(**over):
    base = dict(whatsapp_token="secret-token", whatsapp_agent_name="news-agent",
                whatsapp_bridge_url="http://whatsapp-bridge:3399")
    base.update(over)
    return dataclasses.replace(Settings(), **base)


async def test_whatsapp_success():
    sio = FakeSio({"ok": True, "messageId": "mid-123"})
    d = Delivery(channel="whatsapp", target="351961050313@c.us")
    out = await deliver(d, "hello", settings=_settings(), sio_factory=lambda: sio)

    assert out == "mid-123"
    assert sio.sent["event"] == "sendMessage"
    assert sio.sent["data"] == {"targetId": "351961050313@c.us", "text": "hello"}
    assert sio.sent["namespace"] == "/agent"
    assert sio.auth == {"agentName": "news-agent", "token": "secret-token"}
    assert sio.disconnected  # always cleans up


async def test_whatsapp_negative_ack_raises():
    sio = FakeSio({"ok": False, "error": "not linked"})
    d = Delivery(channel="whatsapp", target="x@c.us")
    with pytest.raises(DeliveryError) as ei:
        await deliver(d, "hi", settings=_settings(), sio_factory=lambda: sio)
    assert "rejected" in str(ei.value)
    assert sio.disconnected


async def test_whatsapp_missing_token_refuses():
    d = Delivery(channel="whatsapp", target="x@c.us")
    with pytest.raises(DeliveryError) as ei:
        await deliver(d, "hi", settings=_settings(whatsapp_token=""), sio_factory=FakeSio)
    assert "WHATSAPP_TOKEN" in str(ei.value)


async def test_bus_delivery_publishes():
    bus = FakeBus()
    d = Delivery(channel="bus", target="dashboard")
    out = await deliver(d, "the result", settings=_settings(), bus=bus, cid="wf-9")

    assert out == "1-0"
    stream, env = bus.published[0]
    assert stream == "stream:dashboard"
    assert env.payload.data == {"output": "the result"}
    assert env.header.cid == "wf-9"


async def test_unsupported_channel_raises():
    # bypass DSL validation to exercise the runtime guard directly
    d = Delivery.model_construct(channel="carrier-pigeon", target="x")
    with pytest.raises(DeliveryError):
        await deliver(d, "hi", settings=_settings())
