# Resource Model — a structured management plane for Patron

**Status:** Design (2026-07-01). Not yet built. This is the trunk; the per-feature designs in
[`designs/`](designs/) are branches that plug into it.

## 1. The problem this fixes

Patron has been growing **ad-hoc, per-field UI**: a WhatsApp target dropdown, an MCP-tools
floating panel, a persona dropdown, a Template Studio, and (requested) a Triggers panel. Each
was hand-coded as its own `control` branch in `props-panel.js` plus its own bespoke admin
endpoint. There is **no unifying model** for "how do we manage new and existing *things*"
(Agents, Triggers, MCP Tools, Presets, WhatsApp targets, Recipes, …). Every new entity means
another button and another one-off panel. That does not scale and it isn't coherent.

## 2. The principle — extend the Block insight to a Management plane

The project already has one load-bearing idea:

> **A Block is self-describing metadata; the editor is a thin generic view.** Add a field once
> in Python (`ConfigField`) → it renders in the Properties panel, lowers to the runtime record,
> and executes — with **zero per-field UI code**. (See `composer/schema.py`, `blocks.py`, and
> `/composer/catalog`.)

We have been **violating** that principle for management — hand-coding each picker instead of
declaring it. The fix is to apply the *same* principle to a second plane:

- **Authoring plane** — Blocks on the canvas compose **one** agent. Already metadata-driven.
- **Management plane** — browse / CRUD over **collections**: Agents, Triggers, MCP Tools,
  Presets, WhatsApp targets, Recipes. Today ad-hoc; **should be metadata-driven the same way.**

A **Resource** is to the management plane what a **Block** is to the authoring plane: a
self-describing descriptor. The editor renders it with **two** generic UIs and never another
bespoke one.

## 3. The Resource descriptor

Declared once, in Python, in `agent_runtime` — the single source of truth (Patron is a thin
client; there may be several clients). Shape (illustrative):

```python
@dataclass
class ResourceDescriptor:
    id: str                      # "agent" | "trigger" | "mcp-tool" | "preset" | "wa-target" | "recipe"
    label: str                   # "Agent", "Trigger", …
    icon: str                    # icons/*.svg (reuse the block-icon convention)
    identity: str                # the key field ("uid", "job_id", "name", "id", "slug")
    schema: BlockSchema          # the fields — REUSE ConfigField/BlockSchema from the block model
    capabilities: set[str]       # {"list","get","pick","create","update","delete", + custom actions}
    source: ResourceSource       # SDK-backed adapter (see §4) — how list/get/create/… actually run
```

`capabilities` is the crux: **read-only catalogs declare `{"list","pick"}`; authored entities
declare full CRUD; special actions are just extra capability verbs** (`deploy`, `pause`,
`resume`, `run`, `import`, `insert`). Same descriptor, same UI, different declared verbs.

Because `schema` reuses `ConfigField`/`BlockSchema`, an **Agent is literally a Block** and its
manager form is the same metadata that already drives the Properties panel. A Trigger's schema
is `{cron, timezone, trigger_type, agent_id}`. A Recipe's is `{name}`. No new schema language.

## 4. SDK-backed sources — the composition backbone

**Every service already ships an SDK, built precisely so these can be composed.** A Resource's
`source` binds to its service's SDK (or, where the transport is a protocol/bridge, the existing
client). This is what makes management *composition* rather than HTTP glue.

| Resource         | Service (port)            | SDK / client (verified present)                                   |
|------------------|---------------------------|-------------------------------------------------------------------|
| **Agent**        | agent_runtime (6817)      | its own admin API + registry (in-process); `agent_bus_client` for events |
| **Trigger**      | agent_scheduler (6816)    | `agent_scheduler/sdk/python/agent_scheduler_client.py`            |
| **Preset**       | agent_server (7701)       | `agent_server/sdk/python/agent_server_sdk/client.py`             |
| **MCP Tool**     | mcp-service (4950)        | standard MCP protocol client → `agent_runtime/mcp_client.py` (`list_tools`) |
| **WhatsApp target** | whatsapp bridge (3399) | Socket.IO `/agent` client (already used in `admin.py::_fetch_whatsapp_targets`) |
| **Recipe**       | patron-local              | serve.py file store (`data/recipes/`) — no remote service        |
| **Bus stream**   | agent_bus (6379/6815)     | `agent_bus_client` (`BusClient`) — future                        |

`ResourceSource` is a thin adapter interface — `list()`, `get(id)`, `create(body)`,
`update(id, body)`, `delete(id)`, `action(id, verb, body)` — each implemented **once per
service against that service's SDK**. agent_runtime becomes the composition point: it already
imports `agent_bus_client`, `mcp_client`, and talks to agent_server/scheduler; the SDKs make
the scheduler/agent_server adapters first-class instead of raw `httpx` calls (which is what the
bespoke endpoints do today).

> Migration note: the current bespoke endpoints (`_fetch_mcp_tools`, `_fetch_presets`,
> `_fetch_whatsapp_targets`) are raw calls. Under this model they become the `list()` methods of
> the `mcp-tool` / `preset` / `wa-target` `ResourceSource`s — and the preset/scheduler ones
> should call the **SDK** rather than hand-rolled `httpx`.

## 5. The two generic UIs (and never another bespoke one)

### 5a. Resource Picker — a field control `resource-ref:<type>`
A block field whose value **references** a resource declares `control="resource-ref"` +
`kind="<resource-id>"` (e.g. `resource-ref` + `mcp-tool`, or `preset`, or `wa-target`). The
Properties panel renders **one** generic picker: a dropdown (single) or checklist (multi),
sourced from that resource's `list()`, with a "type an id" escape hatch and offline text
fallback. This **replaces** the three hand-written branches (`whatsapp-target`, `mcp-tools`,
`preset`) with a single mechanism keyed by `kind`.

Multiplicity (single vs multi), grouping (Groups/Contacts), and the id-vs-label display all
come from the resource descriptor + `ConfigField`, not from bespoke code.

### 5b. Resource Manager — one floating panel
A single floating jsPanel (the shell already proven by the MCP Tools panel:
search + list + row-actions + workspace-persisted position) that renders **any** resource
collection from its descriptor:
- **list** rows (identity + summary columns from the schema),
- **search/filter**,
- **create / edit** form (rendered from `schema` — the same `ConfigField` renderer as the
  Properties panel),
- **row actions** = the resource's capability verbs (`delete`, plus `deploy`/`pause`/`run`/
  `import`/`insert`…).

This **replaces** the bespoke MCP panel, the requested Triggers panel, and the separate Agent
admin UI. Opened either from a `resource-ref` field's "manage…" affordance or from a top-level
"Manage ▸ Agents / Triggers / Tools / Recipes" menu.

### 5c. Consolidate and redesign — do NOT replicate each service's admin UI
This is explicit: the Resource Manager is **not** a port of the scheduler admin page + the
agent_server admin page + the runtime admin frontend stitched into Patron. That would just move
the sprawl. The goal is a **single, simpler, consistent UX** over *all* resources — one list
idiom, one search, one schema-driven form, one set of row-action verbs — that **preserves the
features** of the individual admin UIs without inheriting their divergent, per-service designs.

Consequences:
- The per-service admin UIs (agent_scheduler `frontend/`, agent_server admin, agent_runtime
  admin frontend) are **references for *what capabilities exist*** (e.g. the scheduler's 5-cell
  cron editor + live preview, the runtime's active/inactive toggle + consistency view), not
  templates to copy pixel-for-pixel. Harvest the *feature*, drop the bespoke chrome.
- "Without losing features" is a hard constraint: before a per-service admin UI is retired in
  favour of the Manager, enumerate its capabilities and ensure each maps to a descriptor
  capability verb or a schema field. Nothing silently dropped.
- Simplification comes from **uniformity** (every resource looks/behaves the same) and from
  **grounding** (pick from reality instead of typing ids), not from removing capability.
- Net effect: N divergent admin UIs → **one** Manager surface. Fewer surfaces to learn, build,
  and maintain — a redesign, not a re-implementation.

## 5d. Block ↔ admin view: one surface, generated-by-default, custom-when-warranted

Direction (2026-07-01): move toward a **1:1 block ↔ admin panel** relationship, which **dissolves
the generic Properties panel**. There is no longer a shared key/value Properties panel *and*
separate admin panels — there is **one surface per block**: *the block's admin view*, opened by
**double-clicking the block** (on canvas) or by opening a row in the collection Manager (§5b).
Same surface, two entry points.

**The hard rule that keeps this from regressing into per-block sprawl** (the very thing this whole
model exists to prevent): the **schema stays the contract**. Concretely:
- The block declares its `schema` (ConfigFields) — still the single source of truth for
  validation, lowering, grounding, deploy.
- Its admin view is **auto-generated from that schema by default** — every block gets a working
  panel *for free* (the "declare once" property is preserved; no hand-coding required).
- A block **MAY override** the default with a **custom view component** — a dashboard-style
  component instead of a key/value list — when the generic form isn't good enough. The override
  still reads/writes the same schema-backed fields. Most blocks use the default; high-value blocks
  (e.g. Agent) ship a custom view.

**Generated-by-default + custom-by-exception.** Fully-bespoke-per-block is explicitly rejected — it
is the sprawl this model was created to kill.

**The admin view is where the Management interface lives.** A dashboard component can show config
**and** live management state together — last run, next fire, health, trace, enable/disable — which
a key/value list cannot. This is the "functional vs Management interface" split (from the composer
redesign notes) finally given a home, and it's the strongest reason to prefer the component look.

**Instance vs collection (different scopes, they compose):** the per-block admin view edits **one**
instance (double-click). "Manage new and existing" across **many** (Agents, Triggers, Tools) is the
collection Manager (§5b); opening a row there launches **the same** per-block admin view. Nothing is
duplicated.

**Canvas node as a simple card (NON-URGENT, non-blocking, low-cost):** beyond the admin *panel*, the
on-canvas node body can render as a clean **card** — a headline value, a small badge, a subtitle —
*not* charts/analytics. This is drawable with plain **litegraph canvas-2D** via `onDrawForeground(ctx)`
(the same hook already used for `drawField`, the title bar, connector borders) — no DOM overlays, no
charts. Two metadata-driven rules make it cheap and consistent:
- **Show only fields changed from their default** (`value !== ConfigField.default`) — the *interesting*
  config, not the full key/value list.
- **Per-block card template** — each block declares which field is the headline / subtitle; default
  template = "changed-from-default rows". Same generated-by-default + custom-by-exception rule as §5d.

Explicitly **not critical for anything to work** — deprioritized. Advance the functional model first;
the block look is a polish pass that can land anytime.

## 6. The `/resources/catalog` contract

Symmetric to `/composer/catalog` (which serves block schemas). agent_runtime serves resource
descriptors so the editor renders generic managers/pickers with no hard-coded knowledge:

```
GET /resources/catalog            -> { resources: [ ResourceDescriptor(json), … ] }
GET /resources/<id>               -> list()          (the pick/list source)
GET /resources/<id>/<key>         -> get(key)
POST /resources/<id>              -> create(body)
PUT/PATCH /resources/<id>/<key>   -> update(key, body)
DELETE /resources/<id>/<key>      -> delete(key)
POST /resources/<id>/<key>/<verb> -> action(key, verb, body)   # deploy/pause/resume/run/import/…
```

Patron's `serve.py` proxies `/resources/*` same-origin (the browser is gated under `/patron`
and can't reach the localhost-bound services directly) — exactly the pattern already used for
`/composer/*`, `/admin/channels/*`.

## 7. Entity table (what we manage, and how)

| Resource        | Identity | Capabilities                                  | Today (ad-hoc)                    |
|-----------------|----------|-----------------------------------------------|-----------------------------------|
| **Agent**       | uid      | list · get · create · update · delete · **deploy** · **import** | separate admin UI + deploy bridge |
| **Trigger**     | job_id   | list · get · create · update · delete · **pause/resume/run** | *requested*                       |
| **MCP Tool**    | name     | list · **pick** (read-only)                   | bespoke floating panel            |
| **Preset**      | name     | list · **pick** (read-only)                   | bespoke dropdown                  |
| **WhatsApp target** | id   | list · **pick** (read-only)                   | bespoke dropdown                  |
| **Recipe**      | slug     | list · get · create · delete · **insert**     | *designed*                        |
| RAG domain / TTS voice / Bus stream | — | list · pick (future)               | —                                 |

Adding a new manageable thing (a RAG domain catalog, TTS voices, Bus streams) = **declare one
descriptor in Python**. Zero UI work. That is the whole point, and the twin of the block
insight.

## 8. How the four already-designed features slot in

The four parallel design spikes were really **instances of this model**, which is the clearest
evidence the trunk is right:

- **Round-trip IN** ([`designs/round_trip_import.md`](designs/round_trip_import.md)) = the
  **Agent** resource's `import` capability (record → graph, inverse of `lower_graph`).
- **Triggers management** ([`designs/triggers_management.md`](designs/triggers_management.md)) =
  the **Trigger** resource (full CRUD + pause/resume/run), backed by the scheduler SDK.
- **Recipes / block library** ([`designs/recipes_library.md`](designs/recipes_library.md)) =
  the **Recipe** resource (patron-local, `insert` capability).
- **Multi-agent execution**
  ([`designs/multi_agent_execution.md`](designs/multi_agent_execution.md)) is **orthogonal** —
  it's runtime execution (`lower.py`/`runner.py`), not management — but it's captured so it
  isn't lost.

Instead of four features with four UIs, it's **one framework + four declarations** (plus the
runtime work for multi-agent).

## 9. Migration path (no big-bang)

1. Land `ResourceDescriptor` + `ResourceSource` + `/resources/catalog` in agent_runtime, with
   the SDK-backed adapters (scheduler, agent_server, mcp, wa-bridge, recipe-file).
2. Build the **generic Resource Picker** control; migrate `whatsapp-target` / `mcp-tools` /
   `preset` onto it (delete the three bespoke branches). Net **less** code.
3. Build the **generic Resource Manager** panel (reuse the MCP-panel shell); add **Agent**,
   **Trigger**, **Recipe** managers as descriptors. The bespoke MCP panel becomes a descriptor.
4. Retrofit the separate admin frontend into the Agent manager over time.

## 10. Open decisions

- **Where descriptors live vs SDK dependency:** the descriptors + adapters live in agent_runtime,
  which must depend on each service SDK (`agent_scheduler_client`, `agent_server_sdk`,
  `agent_bus_client`). Confirm the packaging (vendored vs installed) per the existing bus-client
  precedent.
- **Schema reuse boundaries:** an Agent's schema == the Agent Block schema. A Trigger's schema
  overlaps the Trigger Block. Decide whether the resource schema *is* the block schema or a
  parallel declaration (recommend: reuse, single source).
- **Auth/scope:** everything is localhost-bound behind the OAuth proxy today. If the management
  plane is ever widened, capability verbs (delete/deploy/pause) need an auth gate.
- **Tag-based tool scoping** (the pending mcp-service item) becomes a `list()` filter on the
  **MCP Tool** resource — one more reason to have the model before wiring more one-offs.
