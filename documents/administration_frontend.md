# Administration Frontend — Specification

> **Status:** IMPLEMENTED (2026-06-28). Served from the agent_runtime container at `/`
> (admin API under `/admin`). Defines the **missing** administration
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

### 4.0 Identity & naming — `uid` + `name` (foundational)

**Decided.** An agent's identity is a **stable, server-assigned `uid`** (immutable for the
life of the record); its human label is a **mutable `name`**. This is what makes
rename-in-place possible — the scheduler's "id is immutable, rename = recreate" rule does
**not** apply to agents.

- **`uid`** — authoritative identity and **routing key**. Server-generated on first create
  (e.g. `agt_<short-random>`), never changes, never reused. Everything that points at an
  agent points at the `uid`.
- **`name`** — friendly, editable, shown in every UI. Renaming updates the same record.
  Uniqueness is a nicety, not identity (the `uid` disambiguates).
- **The friendly name is propagated for display** — denormalised *alongside* the `uid`
  wherever an agent is referenced, so each frontend can label things without a
  cross-service lookup. The **agent record is authoritative** for the current name; a
  carried name is a **snapshot** and may go stale after a rename. Frontends that must show
  the live name resolve `uid → name` from agent_runtime; the carried name is a
  convenience/fallback label.

**Cross-service ripple (this is a contract change, not UI-only):**

| Place | Carries | Role |
| --- | --- | --- |
| Agent record | `uid` (immutable) + `name` (editable) | source of truth |
| Scheduler job `event_data` | `agent_uid` (routing) + `agent_name` (display snapshot) | farm resolves by `agent_uid` |
| Patron graph | the deployed `uid` + `name` | re-deploy sends the `uid` → **updates the same record**, never a duplicate |
| Run events (`agent-runtime-runs`) | `agent_uid` + `agent_name` | observability labels |

**Migration:** trivial at this scale. There are only a couple of hand-written records
(`news-demo`, `news-morning-ai`) and a job or two. Assign each record a `uid` (a UUIDv4),
keep its `name`, and update the handful of referencing jobs/graphs in one pass. No
backward-compatibility window — there is nothing to phase out.

### 4.1 Record management (CRUD)

- **List** all agent records as a table with at-a-glance columns: `id`, `description`,
  `trigger.type`, `brain.persona`, tool summary (`tools.server` + count of `tools.allow`),
  `delivery.channel` → `target`, and a **link status** badge (see §4.3).
- **Inspect** one record: the full materialised DSL (read-only), plus a "raw YAML" view.
- **Create** a record via a form mapped to the DSL (§4.4), or by pasting/​uploading YAML/JSON.
- **Edit** an existing record in the same form, pre-filled. `uid` is **read-only**
  (immutable identity); **`name` is editable in place** — a rename updates the same
  record (§4.0), it does not create a new one.
- **Delete** a record — **hard-delete**: the record is removed permanently (file + live
  registry). Gated by a typed confirmation and an explicit warning if a scheduler job
  still targets its `uid` (§4.3). Reversible *disabling* is a **separate** future feature,
  not part of Delete. Requires the new `DELETE` endpoint (§5).
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
- **Identity (`uid`) is immutable** and enforced read-only in the UI; the **`name` is
  editable in place** (§4.0). The routing key is always the `uid`, never the name.

### 4.3 The job↔agent seam (cross-reference)

Read the Scheduler Agent API (`GET http://agent-scheduler-app:6816/jobs`) and the agent
registry, then render the linkage so creation mistakes are caught **at author time, not
fire time**:

- Per **agent**: list scheduler jobs whose `event_data.agent_uid == uid` (with trigger +
  next-run). Badge **"orphan"** if none → the record will never be triggered.
- Per **agent**: badge **"dangling job"** surfaced on the *job* side — a job whose
  `event_data.agent_uid` resolves to no record (the farm would silently drop it). The
  job's carried `agent_name` helps a human recognise what it *meant* to point at.
- A top-level **"Consistency" panel**: all dangling jobs and all orphan agents in one
  place. This is the single most valuable view for "make creation of jobs and agents work".

> Cross-referencing is **read-only** toward the scheduler. The frontend never mutates
> jobs; it may deep-link to the Scheduler UI to fix one.

### 4.4 Record form ↔ DSL field map

The create/edit form is a thin, validated projection of `AgentRecord`
([dsl.py](../src/agent_runtime/dsl.py)). Required: `version`, `name`, `brain`, `delivery`
(`uid` is assigned by the server on create).

| Form field | DSL path | Notes / constraints |
| --- | --- | --- |
| Version | `version` | `major.minor`; runtime accepts major `0.x` only |
| Uid | `uid` | server-assigned, immutable, read-only; the routing key (§4.0) |
| Name | `name` | human label, editable in place; shown in every UI |
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
| `POST` | `/admin/agents` | **create** a record; server assigns + returns the `uid` | `name` need not be unique (§4.0) |
| `DELETE` | `/admin/agents/{uid}` | remove a record (file + live registry) | `404` if absent; `204` on success; atomic file unlink + registry drop |
| `GET` | `/admin/agents?detail=1` | **rich list** (full/summary records, not ids) | avoids N+1 GETs to render a table; includes `uid` + `name` |
| `POST` | `/admin/agents/validate` | **dry-run** validate a candidate record | returns `{ok, errors[]}`; never writes |
| `GET` | `/admin/runs?agent_uid={uid}&limit=N` | recent run events from `agent-runtime-runs` | read the runs stream; supports the observability views |
| `GET` | `/admin/consistency` | dangling jobs + orphan agents | join scheduler `/jobs` (by `agent_uid`) with the registry |

**Keying:** all record routes key on the immutable `uid` (replacing today's `{id}`).
Create assigns the `uid`; `PUT /admin/agents/{uid}` updates that record (a `name` change
is just a field update — no new file). `PUT` should report **created vs replaced** so the
UI can warn/diff. The localhost-bound + nginx-gated security model is unchanged (§7); if
the surface widens, add the `ADMIN_TOKEN` bearer noted in `admin.py`.

> Storage note: with `uid` as identity, the on-disk filename should be `<uid>.yaml` (not
> `<name>.yaml`) so a rename never moves the file. The host `data/agents/` bind mount stays
> the source of truth.

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

- **Served from the `agent_runtime` container itself** — the same FastAPI app that already
  exposes the admin API (`agent-runtime-app`, `127.0.0.1:6817`). No new service/container.
  Today that app only mounts the `/admin` router and `/health`; this adds (a) a new
  `frontend/` dir baked into the agent_runtime image via `COPY frontend/ ./frontend/`
  (exactly like the scheduler), and (b) a `StaticFiles` mount serving it at the app root
  (`/`). The page derives its API base from `window.location`, so it works both directly
  and behind a reverse-proxy path prefix.
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
- **Not disable/enable** — a reversible off-switch (pause an agent without deleting it) is
  a worthwhile but **separate** future feature; this spec's Delete is hard-delete only.

## 9. Open decisions

1. **Consistency join location:** *Built server-side* — `GET /admin/consistency` joins the
   scheduler's `/jobs` (by `agent_uid`) with the registry inside agent_runtime. Chosen over
   the client-side lean for **CORS reliability**: the UI is same-origin to agent_runtime,
   which reaches the scheduler over `logus2k_network` (no browser cross-origin). The
   coupling is one read-only env-configured URL (`SCHEDULER_URL`); it degrades gracefully
   (`scheduler_ok:false`) if the scheduler is down.
2. **Runs backing:** read the `agent-runtime-runs` stream live on each request vs a small
   rolling index keyed by `agent_uid`/`cid`. Start with live reads (bounded `limit`), add
   an index only if needed.
> **Decided (this thread):**
> - **Delete** = hard-delete + confirm; reversible *disable* is a separate future feature.
> - **Identity** = server-assigned immutable `uid` (**UUIDv4**, file `<uid>.yaml`, may be
>   displayed shortened); `name` editable in place; routing keys on `uid`, `name`
>   denormalised for display (§4.0).
> - **Migration** = none needed at this scale — assign uids to the few existing records and
>   update their references in one pass (no compatibility window).
> - **Console** = a **separate page** — its own admin frontend served by `agent_runtime`
>   (§7), **not** merged into the Scheduler UI. The consistency view (§4.3) still
>   cross-references scheduler jobs read-only; it just lives on its own page.
</content>
