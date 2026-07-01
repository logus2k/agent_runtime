# Design: Triggers Management

**Status:** Design (2026-07-01). An instance of the [Resource Model](../resource_model.md) — the
**Trigger** resource (full CRUD + pause/resume/run), backed by the agent_scheduler SDK. "Triggers"
== scheduler jobs.

## Grounding
- **Trigger node** carries `agent_id`, `trigger_type` (`schedule|channel`), `cron`, `timezone`.
- **Deploy already owns the job upsert** (`serve.py::_deploy`): PUT agent → get `uid` → upsert job
  with `job_id == agent_id`, `event_data={agent_uid, agent_name}`, `target_stream_id="agent-runtime"`,
  `event_type="schedule.fired"`; POST `/jobs`, PATCH on 409.
- **Scheduler API**: `GET/POST /jobs`, `GET/PATCH/DELETE /jobs/{id}`, `POST /jobs/{id}/{pause|resume|run}`.
  PATCH requires `trigger_type`+`trigger_args` **together**. Cron validated server-side (croniter),
  timezone via ZoneInfo (422 on bad). JobView: `{job_id, trigger, trigger_args{cron_expression,
  timezone}, next_run_time, event_data, paused, …}`.
- **Scheduler admin UI** (`agent_scheduler/frontend/index.html`) has the 5-cell cron editor +
  live preview + tz datalist — **harvest the feature, not the chrome** (per Resource Model §5c).

## Scope recommendation — full management panel (not a bare picker)
A minimal "pick an existing job into the node" is a 3-line write, not worth a control. The real
pain is management: list, edit cron/tz, pause/resume, delete, see next-run. Build the floating
**Triggers** panel (mirroring the MCP-panel shell); include "Bind to node" as one action inside it
(so the picker is a subset, for free).

## Endpoints — same-origin proxies in serve.py → SCHEDULER_URL
Go **directly to the scheduler** (source of truth for jobs; `_deploy` already does). Add:
`GET/POST /admin/scheduler/jobs`, `GET/PATCH/DELETE /admin/scheduler/jobs/{id}`,
`POST /admin/scheduler/jobs/{id}/{pause|resume|run}`. serve.py is stdlib http.server → add
`do_PATCH`/`do_DELETE` (don't exist yet) + `_proxy_patch`/`_proxy_delete` (near-copies of
`_proxy_post`). **Preserve upstream status codes** (201/204/404/409/422) so the UI can react.
*(Under the Resource Model these become the Trigger resource's `ResourceSource` methods, ideally
via `agent_scheduler_client` SDK rather than raw httpx.)*

## UI wiring
Catalog one-liner: Trigger `cron` field control `text` → `"trigger-manager"` (like `mcp-tools` on
`tools_allow`). props-panel `trigger-manager` branch = read-only summary (`"0 7 * * * · Europe/
Lisbon"`) + `…` → `openTriggersPanel(node, onApply)`. New `js/triggers-panel.js` mirrors the MCP
panel (state, `savedTrRect`/`stashTrRect`, `ensureTriggersPanel`, workspace-persisted rect wired
into app.js `collectWorkspace`/`applyWorkspace` as `panels.tr`). List view: rows with `job_id`,
human `trigger`, `next_run_time`, paused badge, actions Pause/Resume/Run/Edit/Delete/**Bind**;
highlight the row whose `job_id === node.agent_id`. Create/Edit form: 5-cell cron + live preview +
tz datalist (server validates; 422 surfaced inline). All node writes via `commitValue`.

## Write model (avoid double-management)
- **Management ops (edit-cron/pause/resume/delete/run) write immediately** to the scheduler — they
  act on *live jobs*.
- **The node's cron/timezone/agent_id remain the authoritative Deploy spec.** `_deploy` still
  upserts from the node.
- **Edit-then-Bind writes BOTH** (PATCH live job + `commitValue` cron/tz/agent_id onto node) so the
  next Deploy is a no-op re-upsert, not a silent revert. The panel never becomes a second divergent
  writer of the deployed job.

## Risks
- **`job_id` vs `agent_uid` identity**: binding a job whose `job_id ≠ node.agent_id` → Deploy POSTs
  a *second* job, orphaning the bound one. Mitigate: binding copies `job_id`→`agent_id`; creating
  for this node forces `job_id = agent_id`; warn on divergence; optionally flag via
  `GET /admin/consistency`.
- **Node ↔ live-job drift**: editing the job changes the live schedule; node holds old cron until
  Bind; later Deploy reverts. Mitigate with edit-then-Bind + a "node differs from live job" row hint.
- **Deleting a job a deployed agent depends on**: confirm dialog naming `event_data.agent_name`;
  don't cascade-delete the agent.
- **Validation**: delegate to server; live preview is display-only (never a client accept/reject gate).
- **PATCH coupling**: always send `trigger_type`+full `trigger_args` even for a tz-only change.
- **Missing `do_PATCH`/`do_DELETE`** → browser 501 + silent panel failure. Cover in proxy tests.

## Critical files
`patron/serve.py` (scheduler proxies + `do_PATCH`/`do_DELETE`) · `patron/js/props-panel.js`
(`trigger-manager` branch) · `composer/blocks.py` (Trigger `cron` control) · `patron/js/app.js`
(`panels.tr` rect + register script) · `agent_scheduler/frontend/index.html` (cron-editor feature ref).
