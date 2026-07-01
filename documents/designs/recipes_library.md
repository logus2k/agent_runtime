# Design: Recipes / Block Library

**Status:** Design (2026-07-01). An instance of the [Resource Model](../resource_model.md) — the
**Recipe** resource (patron-local; `insert` capability).

## Key insight
A recipe is a **named, server-persisted, multi-slot clipboard**. litegraph's
`copyToClipboard`/`pasteFromClipboard` (`vendor/litegraph/litegraph.js:7234`/`:7293`) already
solve the hard parts: relative-id capture, dangling-link pruning (only internal links), fresh id
assignment via `graph.add`, and connect-by-slot. Recipes reuse that logic; they are **not** wired
to the composer `Composite` block (that's a runtime collapse-to-one-block concept, deferred).

## v1 = raw litegraph subgraph recipes
Save a selection of nodes+links as a named recipe; instantiate later expanded into the current
graph with fresh ids + an offset. After insertion it's ordinary nodes → compiles via
`/composer/compile` unchanged. **Zero runtime risk** — only serve.py + app.js change.

*(v2, deferred: "collapse recipe into a Composite node" once the runtime grows the Composite
inner-graph contract. The v1 JSON is forward-compatible — it stores the full node/link graph.)*

## Storage (serve.py) — mirror the workspace pattern
`data/recipes/`, one JSON per recipe keyed by slug. Endpoints (added like existing `API` branches):
- `GET /api/recipes` → `[{name, slug, node_count, updated}]` (index; `[]` if dir absent)
- `GET /api/recipes/<slug>` → full recipe / 404
- `PUT /api/recipes/<slug>` → atomic write (reuse `tmp`+`os.replace`)
- `DELETE /api/recipes/<slug>` → remove (idempotent); needs new `do_DELETE`

**Sanitize the slug** (`[a-z0-9-]` only, realpath-verify under `data/recipes/`) — serve.py binds
`0.0.0.0` behind the OAuth proxy, so path-traversal matters. This is the one new security surface.

### Recipe JSON
```json
{ "version":1, "name":"…", "slug":"…", "updated":"…",
  "nodes":[ /* node.serialize() outputs; array index = relative id */ ],
  "links":[ [originRelId, originSlot, targetRelId, targetSlot, originNodeId], … ] }
```
`links` = litegraph's 5-tuple relative form; only **internal** links (both endpoints selected)
are captured → dangling links pruned for free.

## SAVE flow (app.js)
`saveRecipeFromSelection()`: read `lgcanvas.selected_nodes`; prompt name → slug; build payload
via a factored `collectSelection()` (≈25 lines ported from litegraph 7239-7286: assign
`_relative_id`, `node.clone().serialize()`, walk internal input links to 5-tuples) — **prefer
this over reusing `copyToClipboard`** so Ctrl-C stays independent; `PUT /api/recipes/<slug>`
(relative path); refresh palette.

## INSERT flow (app.js) — the id-remap
`insertRecipe(recipe, dropPos)` — a de-localStorage'd port of `pasteFromClipboard`:
1. `posMin` = componentwise min of node positions; anchor = drop point (or view-center + offset).
2. For each node_data: `LiteGraph.createNode(type)` (skip+warn if unregistered) → `configure` →
   offset pos → `graph.add(node)` (**assigns fresh id**) → keep in `newNodes[i]`.
3. For each `[oRel,oSlot,tRel,tSlot]`: `newNodes[oRel].connect(oSlot, newNodes[tRel], tSlot)`
   (**mints fresh link id**).
4. `selectNodes(newNodes)`; `setDirtyCanvas`; `scheduleSave()`.

Correctness: never trust serialized ids — `graph.add` assigns node ids, `connect` assigns link
ids, relative indices bridge them. No manual id math, no collisions.

## UI
- **Toolbox "Recipes" palette section** (primary): fetch index at boot; render draggable items
  with a `recipe:<slug>` drag token; extend the canvas `drop` handler to fetch + `insertRecipe`;
  small delete affordance per item.
- **File menu**: "Save Selection as Recipe…", "Manage Recipes…".

## Tests
serve.py: PUT/GET/DELETE round-trip; slug sanitizer rejects `../`/absolute/non-`[a-z0-9-]`;
missing dir → `[]`. JS (manual/dev): 3-node selection → 3 nodes/2 links (relative form);
insert into non-empty graph → ids > existing, no collision, offset+selected; unregistered type →
skip+warn; links to non-selected nodes absent.

## Risks
Id collisions (mitigated: never trust serialized ids) · dangling links (avoided at save: internal
only) · path traversal (sanitize slug) · unregistered node types (skip-with-warning per node) ·
versioning (`version:1` stamp) · widget-value drift (handled by `configure`→`syncWidgets`) ·
autosave interaction (`scheduleSave` after insert, not during boot) · clipboard clobbering
(use `collectSelection`, not `copyToClipboard`).

## Critical files
`patron/serve.py` (recipe endpoints + `do_DELETE` + slug sanitizer) ·
`patron/js/app.js` (`collectSelection`, `saveRecipeFromSelection`, `insertRecipe`, drop handler,
palette+menu) · `patron/js/menu.js` (File entries) · `patron/js/agent_nodes.js` (PALETTE contract) ·
`vendor/litegraph/litegraph.js` (reference: 7234/7293, `graph.add`/`connect`).
