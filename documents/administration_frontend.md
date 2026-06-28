# Administration Frontend — Specification

> **Status:** specification only (no code yet). Defines the **missing** administration
> capabilities for `agent_runtime` agent records. Grounded in the current code:
> [src/agent_runtime/admin.py](../src/agent_runtime/admin.py),
> [src/agent_runtime/dsl.py](../src/agent_runtime/dsl.py),
> [src/agent_runtime/registry.py](../src/agent_runtime/registry.py),
> [src/agent_runtime/runner.py](../src/agent_runtime/runner.py).

## 1. Purpose

`agent_runtime` hosts agent records (the runtime DSL) but exposes **no first-class way
to manage them**. Today administration is: hand-edit YAML in `data/agents/` on the host,
or drive a thin, incomplete admin API by hand. This document specifies an
**administration frontend + the backend gaps it requires** so that *creating, viewing,
editing, deleting, and observing* agent records is a coherent, safe, first-class flow —
the same standard the **Scheduler Agent** already meets for jobs.

The bar is parity-of-management with the scheduler UI, plus the things unique to a
runtime farm (run history, the job↔agent seam).

## 2. Current state (gap analysis)

What exists — a **headless, partial** admin API (localhost `127.0.0.1:6817`, prefix `/admin`):

| Method | Route | Behaviour | Limitation |
| --- | --- | --- | --- |
| `GET` | `/admin/agents` | list agent **ids only** | no metadata — can't render a table without N follow-up GETs |
| `GET` | `/admin/agents/{id}` | one full record (defaults materialised) | — |
| `PUT` | `/admin/agents/{id}` | validate + persist `data/agents/{id}.yaml` + live-upsert | no dry-run; path-id must equal record-id (`400` otherwise); **overwrites silently** |
| `POST` | `/admin/reload` | re-read all records from disk | drops in-memory-only ids |
| `GET` | `/health` | liveness | does not report registry size/agent count |

**What is missing (the subject of this spec):**

1. **No DELETE.** A record cannot be removed via the API at all — only by `rm`-ing the
   file on the host and `POST /admin/reload`.
2. **No web UI.** `agent_runtime` has no `frontend/`; it is a headless farm. Contrast the
   Scheduler Agent, which ships a full CRUD admin UI.
3. **No rich listing.** `GET /admin/agents` returns ids only — no description, trigger
   type, delivery target, tool summary, or "is anything scheduled to trigger this?".
4. **No validation surface.** You cannot validate a candidate record without committing
   it (no dry-run); the only feedback is a `422` on PUT *after* deciding to write.
5. **No observability.** Runs are emitted to the `agent-runtime-runs` stream
   (`tool.exec`, `agent.thought`, lifecycle, delivery, errors — keyed by `cid`) but
   nothing surfaces them: no run history, no last-run status, no failure visibility.
6. **No view of the job↔agent seam.** Nothing shows which scheduler jobs target which
   agent, so two failure modes are invisible until firing time:
   - a **dangling job** (`event_data.agent` names a non-existent record → silently dropped by the farm),
   - an **orphan agent** (a record no job ever triggers → never runs).

## 3. Boundaries — how this relates to Patron and the Scheduler UI

This frontend is **management + observability of existing runtime records**. It is *not*
an authoring canvas and *not* a job scheduler.

- **Patron** = authoring. A human draws a graph, compiles it to runtime DSL, and
  **Deploys** (`PUT /admin/agents/{id}`). Patron *creates/updates* records; it does not
  list, edit-existing, delete, or observe. (See [patron deploy bridge] behaviour:
  Deploy overwrites `data/agents/{id}.yaml` wholesale and the id comes from the graph's
  Trigger node, not the scheduler job name.)
- **Scheduler Agent UI** = time triggers. It owns jobs (`stream:agent-runtime` +
  `event_data.agent`). It has no knowledge of whether the named agent exists.
- **This Administration Frontend** = the missing middle: browse/inspect/edit/delete the
  records the farm actually loads, see their run history, and surface the seam to
  scheduler jobs.

Design intent: it **complements** Patron (it may deep-link "Edit in Patron"), and
**cross-references** the scheduler (read-only) — it does not re-implement either.

## 4. Capabilities (the specification)

### 4.1 Record management (CRUD)

- **List** all agent records as a table with at-a-glance columns: `id`, `description`,
  `trigger.type`, `brain.persona`, tool summary (`tools.server` + count of `tools.allow`),
  `delivery.channel` → `target`, and a **link status** badge (see §4.3).
- **Inspect** one record: the full materialised DSL (read-only), plus a "raw YAML" view.
- **Create** a record via a form mapped to the DSL (§4.4), or by pasting/​uploading YAML/JSON.
- **Edit** an existing record in the same form, pre-filled. `id` is **read-only**
  (immutable identity — rename = create-new + delete-old, matching the scheduler's
  job-id rule).
- **Delete** a record, with a typed confirmation and an explicit warning if a scheduler
  job still targets it (§4.3). Requires the new `DELETE` endpoint (§5).
- **Validate (dry-run)** a candidate record and show structured errors **before**
  committing — the same `extra="forbid"` + field validators the loader applies at boot.
- **Reload from disk** (authoritative reset) with a clear note that it drops
  in-memory-only ids.

### 4.2 Safety rules (explicit)

- **Overwrite is never silent.** Editing/Deploying an existing `id` must warn that it
  replaces the stored record wholesale (tools, delivery, prompt refs included) and show
  a **diff** (current vs incoming) before save.
- **Destructive ops confirm.** Delete and overwrite require explicit confirmation.
- **Validation is loud and pre-commit.** Errors surface field-by-field, never as a wall
  of stack trace, and never *after* an unintended write.
- **`id` immutability** is enforced in the UI (read-only) and already by the API
  (`path id != record id` → `400`).

### 4.3 The job↔agent seam (cross-reference)

Read the Scheduler Agent API (`GET http://agent-scheduler-app:6816/jobs`) and the agent
registry, then render the linkage so creation mistakes are caught **at author time, not
fire time**:

- Per **agent**: list scheduler jobs whose `event_data.agent == id` (with trigger +
  next-run). Badge **"orphan"** if none → the record will never be triggered.
- Per **agent**: badge **"dangling job"** surfaced on the *job* side — a job whose
  `event_data.agent` resolves to no record (the farm would silently drop it).
- A top-level **"Consistency" panel**: all dangling jobs and all orphan agents in one
  place. This is the single most valuable view for "make creation of jobs and agents work".

> Cross-referencing is **read-only** toward the scheduler. The frontend never mutates
> jobs; it may deep-link to the Scheduler UI to fix one.

### 4.4 Record form ↔ DSL field map

The create/edit form is a thin, validated projection of `AgentRecord`
([dsl.py](../src/agent_runtime/dsl.py)). Required: `version`, `id`, `brain`, `delivery`.

| Form field | DSL path | Notes / constraints |
| --- | --- | --- |
| Version | `version` | `major.minor`; runtime accepts major `0.x` only |
| Id | `id` | `^[A-Za-z0-9._:-]+$`; immutable on edit |
| Description | `description` | optional |
| Trigger type | `trigger.type` | `schedule` \| `channel` (default `schedule`) |
| Persona | `brain.persona` | non-empty agent_server preset name |
| LLM overrides | `brain.llm` | optional: `temperature/max_tokens/top_p/top_k/min_p` |
| Tools server | `tools.server` | MCP server key; must equal the runtime's configured key |
| Tools allow | `tools.allow[]` | each `^<server>__<tool>$`; **shape-checked only**, not existence |
| Max rounds | `tools.max_rounds` | `>= 1` (default 3) |
| Input template | `input.template` | `{var}` placeholders resolved from `input.vars` |
| Input vars | `input.vars` | JSON object |
| Delivery channel | `delivery.channel` | `whatsapp` \| `bus` \| `tts` |
| Delivery target | `delivery.target` | non-empty |
| (advanced) RAG | `rag` | `rewriter`, `domains[]`, `use_graph` |
| (advanced) Guardrails | `guardrails` | `forbidden[]`, `min_confidence` |
| (advanced) Memory | `memory` | `policy` (`none`\|`thread_window`), `max_turns` |

Per-field help should mirror the validators (e.g. "tools.allow entries must match
`<server>__<tool>`") so the same message appears before submit and on a `422`.

### 4.5 Observability (run history)

Surface the existing `agent-runtime-runs` stream (config `RUNS_STREAM_ID`) — runs are
already emitted, just never shown:

- Per agent: **last run** (status: completed / guardrail-blocked / error, timestamp,
  `cid`).
- A **run timeline** for a `cid`: the ordered events `agent.thought`, `tool.exec` (with
  tool name/args/turn), delivery, and terminal success/error.
- A farm-wide **recent runs** list (newest first) with status filter.
- **Live tail** (optional, later): subscribe to new run events.

> This is read-only telemetry. It does not change execution; a dropped trace never
> blocks a job (runner emits run events best-effort).

### 4.6 Health / ops

- Surface `/health` plus **registry size** (agent count) and last reload time.
- Show the resolved runtime config that affects records: `AGENTS_DIR`, `MCP server key`,
  `FARM_STREAM_ID`, `RUNS_STREAM_ID` (read-only).

## 5. Required backend additions

The UI cannot be built on today's API alone. Minimum additions to `admin.py`:

| Method | Route | Purpose | Notes |
| --- | --- | --- | --- |
| `DELETE` | `/admin/agents/{id}` | remove a record (file + live registry) | `404` if absent; `204` on success; atomic file unlink + registry drop |
| `GET` | `/admin/agents?detail=1` | **rich list** (full/summary records, not ids) | avoids N+1 GETs to render a table |
| `POST` | `/admin/agents/validate` | **dry-run** validate a candidate record | returns `{ok, errors[]}`; never writes |
| `GET` | `/admin/runs?agent={id}&limit=N` | recent run events from `agent-runtime-runs` | read the runs stream; supports the observability views |
| `GET` | `/admin/consistency` | dangling jobs + orphan agents | joins scheduler `/jobs` with the registry server-side (or done client-side) |

Existing endpoints stay as-is. `PUT` should additionally return whether it **created vs
replaced** (so the UI can warn/diff). The localhost-bound + nginx-gated security model is
unchanged (§7); if the surface widens, add the `ADMIN_TOKEN` bearer noted in `admin.py`.

## 6. Screens (UX outline)

1. **Agents** (default): table (§4.1) + "New agent" + per-row Inspect / Edit / Delete /
   "View runs" / (deep-link) "Open in Patron".
2. **Agent editor**: the §4.4 form with live validation (dry-run on blur/submit),
   id read-only in edit mode, diff-on-overwrite, raw-YAML toggle.
3. **Consistency**: dangling jobs + orphan agents, each linking to the fix.
4. **Runs**: recent runs list + per-`cid` timeline.
5. **Health/Config**: read-only ops panel.

## 7. Serving, auth, deployment

Mirror the Scheduler Agent precedent exactly (consistency + reuse):

- Served by the same FastAPI process from a `frontend/` dir at the app root (`/`), so it
  works both directly and behind a reverse-proxy path prefix (derive base from
  `window.location`).
- Public access via the existing nginx + **oauth2-proxy**, gated to **a single owner
  identity** (`logus2k@gmail.com`) using the established `/oauth2/auth-admin` pattern in
  [proxy_server/conf/nginx.conf](../../proxy_server/conf/nginx.conf). No per-request auth
  in-process (localhost-bound; `127.0.0.1:6817`).
- Reuse the scheduler UI's structure (vanilla ES6, class-based app, zero-dependency
  client SDK, light/dark, per-field help, draggable help panel) for a consistent feel.
- A thin **JS/Python client SDK** over the admin API (parity with the scheduler SDKs).

## 8. Non-goals

- **Not an authoring canvas** — graph authoring stays in Patron.
- **Not a job scheduler** — jobs stay in the Scheduler Agent (cross-referenced read-only).
- **Not preset/prompt editing** — personas live in agent_server; the form references a
  preset name, it does not edit cognition.
- **Not MCP tool authoring** — tools live in mcp-service; `tools.allow` is referenced and
  shape-checked, not defined here.

## 9. Open decisions

1. **Consistency join location:** server-side `/admin/consistency` (one call, but couples
   the runtime to the scheduler's URL) vs client-side (UI calls both APIs). Leaning
   client-side to keep the runtime decoupled — the UI already talks to both.
2. **Runs backing:** read the `agent-runtime-runs` stream live on each request vs a small
   rolling index keyed by agent/cid. Start with live reads (bounded `limit`), add an index
   only if needed.
3. **Delete semantics:** hard-delete only, or soft-disable (a `disabled` flag the farm
   skips)? A disable flag would let you stop an agent without losing its definition —
   worth considering alongside the DELETE endpoint.
4. **Standalone vs merged UI:** ship as its own app, or add an "Agents" section to the
   Scheduler UI so jobs and agents are managed in one place. The seam (§4.3) is a strong
   argument for a single combined console; the decoupling principle argues for two.
</content>
