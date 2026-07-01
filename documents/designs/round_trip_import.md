# Design: Round-trip IN ("Import from runtime")

**Status:** Design (2026-07-01). An instance of the [Resource Model](../resource_model.md) —
the **Agent** resource's `import` capability. Makes Patron bidirectional (today it only
authors OUT via deploy).

## Goal
Given a deployed `AgentRecord` (`GET /admin/agents/{uid}`) + its scheduler job, reconstruct a
Patron graph (`trigger → agent → destination` + one flow link) that lowers **back** to the same
record. The exact inverse of `composer/lower.py::lower_graph`.

## Where the reverse mapping lives
New pure/stateless module **`composer/lift.py`**, exported beside `lower_graph`:
```python
def lift_record(record: dict, *, schedule: dict | None = None) -> dict:
    """AgentRecord (+ optional scheduler job) -> a litegraph serialize() graph
    that lowers back to the same record."""
```
Rationale: lowering merges fragments and discards which block produced which field; a clean
inverse only needs the record shape (`dsl.py`) + the block catalog (property names). Keep it one
function beside `lower_graph`, matching the "single source of truth, no client copy" doctrine.

## Two asymmetries to bridge
1. **`agent_id` comes from `record.name`**, NOT `uid`. The Trigger node's `agent_id` lowers to
   record field `id`; admin maps `id ↔ name`. The uid is server identity and appears nowhere in
   the graph.
2. **Schedule lives in the scheduler, not the record.** cron/timezone come from
   `GET :6816/jobs/{job_id}` where `job_id == record.name`. Tolerate 404 (unscheduled agent →
   `schedule=None`).

## Field → node-property map (must equal agent_nodes.js property names == catalog keys)
- **Trigger**: `agent_id`←`record.name`; `trigger_type`←`record.trigger.type`;
  `cron`←`schedule.trigger_args.cron_expression` (else `0 7 * * *`); `timezone`←`…timezone` (else `""`).
- **Agent**: `persona`←`brain.persona`; `temperature`/`max_tokens`←`brain.llm.*`;
  `top_p/top_k/min_p`← **emit only if present**; `input_template`←`input.template`;
  `input_vars`←`json.dumps(input.vars)` (**JSON string**, `"{}"` when empty);
  `tools_allow`←`", ".join(tools.allow)` (drop `tools.server` — re-derived from the `<server>__`
  prefix on lower); `tools_max_rounds`←`tools.max_rounds`; `memory`/`memory_max_turns`←`memory.*`
  (**always emit** — lower always emits memory); `description`←`record.description` (`""` when
  None); `enabled`←`record.enabled`; rag_*/guard_* only when those blocks present.
- **Destination**: `type = record.delivery.channel`; `target`←`delivery.target`;
  `target_name`←`delivery.target_name` (omit if blank).

## litegraph JSON to emit
Three nodes ids `1,2,3`, positional link tuples `[link_id, origin_id, origin_slot, target_id,
target_slot, "flow"]` — match `patron/examples/news-agent.graph.json`. Positions are the
canonical fixture layout (static line, no layout algorithm). `size` is cosmetic (agent_nodes.js
`configure` recomputes width).

## HTTP surface
- `POST /composer/lift` in `composer_api.py`: `{record, schedule?} -> {ok, graph} | {ok:false, errors}`.
  Keep it **pure/stateless** (like `/compile`).
- `serve.py` bridge `GET /api/import?uid=<uid>`: (1) GET record from runtime, (2) GET job from
  scheduler by `record.name` (404-tolerant), (3) POST `/composer/lift`, (4) return `{ok, id, uid, graph}`.
- Also proxy `GET /admin/agents` (list) so the import picker can populate.

## Patron UI
`File → Import from runtime` → pick an agent (chooser overlay) → fetch `/api/import` →
`graph.clear(); graph.configure(j.graph); scheduleSave()` — the exact path `loadNewsAgent` uses,
so `configure`/`syncWidgets` re-fit widths and show loaded values. All fetch paths relative
(survive the `/patron` proxy prefix).

## Tests (fixture-driven, reuse `_agentfixtures.py`)
`tests/test_composer_lift.py`, parametrized over the same graph/golden PAIRS. Core property:
`lower_graph(lift_record(record, schedule=job)) == {ok, dsl: golden.dsl, schedule: golden.schedule}`.
Second assertion: `lower_graph(lifted) == lower_graph(hand_authored_fixture)` (lowering-equivalent;
avoids over-strict exact-graph equality). Plus an endpoint-shape test.

## Risks
- **`tools.server` lossy-but-recoverable**: derived from `allow[0]` prefix. Mixed-server allow
  lists won't round-trip → emit a loud `LiftError` if `tools.server` disagrees with the derived
  prefix (no silent failure). One MCP server per agent today → fine.
- **Emit-only-when-present** for optional `top_p/top_k/min_p` and `rag`/`guardrails`/`target_name`,
  else re-lower adds keys and breaks equality.
- **`memory` always present** on both sides (dsl defaults `none`/20).
- **uid vs name/id**: graph carries `name` in `agent_id`; deploy re-resolves uid by name.
- **Control blocks (Transform/Branch/Loop) can't be reconstructed** from a flat record — a record
  authored with a Transform lifts to a straight 3-node line (documented v0 limitation).

## Critical files
- `composer/lift.py` (new) · `composer_api.py` (`POST /composer/lift`) ·
  `patron/serve.py` (`GET /api/import`, proxy `GET /admin/agents`, scheduler job fetch) ·
  `patron/js/app.js` (File → Import) · `tests/test_composer_lift.py` (new).
