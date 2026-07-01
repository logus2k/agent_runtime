"""composer Phase 0: the Block model + schema + Edge + Catalog.

Pure, offline unit tests — no services. Each block round-trips its schema/config/
validate; the Management interface is present on every leaf; the Edge stamps the bus
envelope header and validates port-schema compatibility.
"""

from agent_bus_client import EventEnvelope

from agent_runtime.composer import (
    Agent,
    Block,
    Bus,
    Catalog,
    Edge,
    TTS,
    Transform,
    Trigger,
    WhatsApp,
)
from agent_runtime.composer.schema import ANY, STRING, BlockSchema, DataSchema, Port


# --- DataSchema: structural compatibility --------------------------------------
def test_dataschema_any_is_universal():
    assert STRING.is_compatible_with(ANY)
    assert ANY.is_compatible_with(STRING)


def test_dataschema_scalar_mismatch_is_incompatible():
    assert not STRING.is_compatible_with(DataSchema(type="number"))


def test_dataschema_object_requires_required_props():
    producer = DataSchema(type="object", properties={"a": STRING, "b": STRING})
    consumer = DataSchema(type="object", properties={"a": STRING}, required=frozenset({"a"}))
    assert producer.is_compatible_with(consumer)
    needs_c = DataSchema(type="object", properties={"c": STRING}, required=frozenset({"c"}))
    assert not producer.is_compatible_with(needs_c)


def test_dataschema_roundtrips_json():
    s = DataSchema(type="object", properties={"a": STRING}, required=frozenset({"a"}))
    assert DataSchema.from_json(s.to_json()) == s


# --- every leaf satisfies the contract -----------------------------------------
ALL_LEAVES = [Agent, Trigger, Transform, WhatsApp, TTS, Bus]


def test_every_leaf_is_a_block_with_schema_and_management():
    for cls in ALL_LEAVES:
        b = cls()
        assert isinstance(b, Block)
        schema = b.get_schema()
        assert isinstance(schema, BlockSchema)
        assert schema.kind and schema.category and schema.label
        # Management interface present (universal default from Block).
        assert b.inspect()["kind"] == schema.kind
        assert b.authorize(object()) is None  # default allow


def test_config_roundtrip():
    a = Agent(config={"persona": "news_curator"})
    assert a.get_config() == {"persona": "news_curator"}
    a.set_config({"persona": "x", "max_tokens": 256})
    assert a.cfg("max_tokens") == 256


def test_required_config_validation_is_loud():
    # Agent.persona is required.
    assert any("persona" in e for e in Agent().validate())
    assert Agent(config={"persona": "p"}).validate() == []
    # Destination.target is required.
    assert any("target" in e for e in WhatsApp().validate())
    assert WhatsApp(config={"target": "x@c.us"}).validate() == []


def test_enum_config_validation():
    bad = Trigger(config={"agent_id": "a", "trigger_type": "nope"})
    assert any("trigger_type" in e for e in bad.validate())


def test_agent_rejects_malformed_input_vars():
    a = Agent(config={"persona": "p", "input_vars": "{not json"})
    assert any("input_vars" in e for e in a.validate())


def test_family_categories():
    assert Agent().category == "Agent"
    assert Trigger().category == "Activity"
    assert WhatsApp().category == "Destination"


# --- Catalog -------------------------------------------------------------------
def test_catalog_lists_every_block_type():
    entries = Catalog().entries()
    kinds = {e["type"] for e in entries}
    assert {"agent", "trigger", "transform", "whatsapp", "tts", "bus"} <= kinds
    agent_entry = next(e for e in entries if e["type"] == "agent")
    assert agent_entry["category"] == "Agent"
    assert agent_entry["ports"]["in"]["schema"]["type"] == "string"
    assert any(c["key"] == "persona" and c["required"] for c in agent_entry["config"])


# --- Edge: bus-envelope header + schema validation -----------------------------
def _edge(src_schema=STRING, dst_schema=STRING):
    return Edge(
        src_block="agent",
        src_port=Port("out", "out", src_schema),
        dst_block="whatsapp",
        dst_port=Port("in", "in", dst_schema),
    )


def test_edge_required_header_is_the_bus_envelope_header():
    assert Edge.required_header() == ("cid", "sid", "sender", "timestamp")


def test_edge_stamp_produces_bus_envelope_with_header_and_payload():
    env = _edge().stamp("hello", cid="c1", sid=7, sender="agent")
    assert isinstance(env, EventEnvelope)
    assert env.header.cid == "c1"
    assert env.header.sid == 7
    assert env.header.sender == "agent"
    assert env.header.timestamp  # UTC ISO string present
    assert env.payload.data == {"value": "hello"}


def test_edge_trace_projects_header():
    e = _edge()
    env = e.stamp("hi", cid="c9", sid=1, sender="agent")
    tr = e.trace(env)
    assert tr.cid == "c9" and tr.sender == "agent"
    assert tr.edge == "agent.out -> whatsapp.in"
    assert tr.to_json()["cid"] == "c9"


def test_edge_validates_schema_compatibility():
    assert _edge(STRING, STRING).validate() == []
    bad = _edge(STRING, DataSchema(type="number")).validate()
    assert bad and "not compatible" in bad[0]


def test_edge_rejects_wrong_port_directions():
    try:
        Edge(src_block="a", src_port=Port("in", "in"), dst_block="b", dst_port=Port("in", "in"))
    except ValueError as exc:
        assert "out" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for a non-out source port")
