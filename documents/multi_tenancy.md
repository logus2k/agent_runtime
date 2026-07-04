# Multi-Tenancy — Architecture & Implementation Plan

**Goal:** turn the platform from a single-operator tool into a **multi-user** one, where each user
owns their own projects/agents/resources and is **isolated** — no user can see, run, observe, or
mutate another user's work — without rewriting the runtime.

**Status:** DESIGN. Nothing here is built yet. This is the foundation to build **before** the Trace
panel (the user chose: multi-tenancy first, then the live Trace/Debug panel).

> This is a cross-cutting document: it spans `proxy_server` (identity), `patron` (authoring +
> proxy), `agent_runtime` (execution + admin API), and `agent_scheduler` / ingress services
> (firing bindings). It lives here because `agent_runtime` is the execution hub.

---

## 1. Current state (verified)

Single-user **by design**, not by accident:

- **Identity exists at the edge.** `proxy_server` runs **OAuth2Proxy v7.15.0** (Google OIDC,
  `--scope=openid email profile`, `--email-domain=*`). After login it can inject the authenticated
  principal upstream (`X-Auth-Request-Email`, `X-Auth-Request-User`). Today access to `/patron` is
  pinned to a **single email** (`logus2k@gmail.com`) — so there is exactly one principal.
- **No ownership anywhere.** A stored project is `{uid, name, description, version, graph, ui,
  updated}` — no `owner`. `GraphRecord`, agent records, resources, and scheduler/ingress bindings
  likewise have no owner field.
- **Endpoints authorize by `uid` alone.** Every `/projects/{uid}/…` route (deploy, undeploy, fire,
  events, delete, get) trusts the `uid` and never asks *who is calling*. `GET /api/projects` returns
  **all** projects.
- **One shared farm, one shared bus.** All projects run in one `agent_runtime` process; isolation is
  already by `record_uid` at the routing layer. Valkey (bus) is shared internal infra.

`serve.py` says this outright: *"Dev-grade… a SINGLE workspace document, no auth… not a multi-tenant
store. Per-user storage are a later refactor when there's a concrete need."* This document is that
refactor.

---

## 2. The isolation model (what "multi-tenant" means here)

A **principal** is an authenticated user (see §3). Every **owned entity** has exactly one owner
principal. Isolation has three faces — all must hold:

| Face | Question | Enforced by |
|---|---|---|
| **Storage** | Whose projects/agents/resources are these? | `owner` field + list scoping |
| **Control** | Who may deploy / fire / delete / undeploy? | ownership check on `/{uid}` routes |
| **Observability** | Who may see runs / the live trace / payloads? | ownership check on `/runs`, `/events` |

### The uid is NOT a secret (design principle)
`uid = uuid4().hex[:12]` (48 random bits) — unguessable by brute force, but uids **leak** (URLs,
logs, screen-shares) and `GET /api/projects` hands them out wholesale. So a uid must **never** be a
capability. The security boundary is **ownership authorization**, not uid-secrecy: once the endpoint
checks `owner == caller`, a known/guessed/leaked uid grants nothing.

### Non-goals
- **No per-tenant runtime process / sandbox.** One farm, logical isolation via `record_uid` + authz
  is sufficient. (A compromised *workflow* — e.g. a future code/Transform block — is a separate
  concern from tenant isolation.)
- **No resource quotas / rate-limits per tenant** in v1 (note as a follow-up).
- **No org/team hierarchy** in v1 — flat per-user ownership; teams are a later layer (§8).

---

## 3. Identity & principal propagation

```
Browser ──▶ nginx (proxy_server) ──▶ OAuth2Proxy ──▶ [authenticated]
                                          │  injects  X-Auth-Request-Email / -User
                                          ▼
                                    Patron serve.py  ──(server-to-server)──▶  agent_runtime admin API
                                          │  forwards X-Patron-User                     │
                                          ▼                                             ▼
                                    reads the header, resolves the PRINCIPAL     trusts the header ONLY
                                                                                 from serve.py (internal)
```

- **Principal key.** Use the OIDC **`sub`** (stable, immutable) as the canonical owner key, and store
  the **email** alongside for display. Email can change; `sub` cannot. OAuth2Proxy exposes both
  (`X-Auth-Request-User` ≈ subject/preferred-username, `X-Auth-Request-Email`). (decided: `sub` — §9.1).
- **Un-pin the allow-list.** Remove the single-email restriction; OAuth2Proxy still gates login to
  the org's Google domain(s) via `--email-domain`, but now admits many principals.
- **Trust boundary.** The identity header is trusted **only** because it arrives via the edge proxy.
  The farm is localhost/internal-network bound — the *only* ingress is proxy → serve.py → farm.
  serve.py must **not** accept a client-supplied identity header; it sets `X-Patron-User` itself from
  the proxy-verified header. The farm accepts `X-Patron-User` only from serve.py's network origin.
- **`principal(request)` helper** on both sides: serve.py derives it from `X-Auth-Request-*`; the
  farm derives it from `X-Patron-User`. A missing/unverifiable principal → **401**.

---

## 4. Data model — add `owner`

Add an `owner` (principal `sub`) + `owner_email` (display) to each **owned** entity, written at
**create/first-save** time and immutable thereafter (transfer is an explicit action, §8):

| Entity | Store | Change |
|---|---|---|
| Patron **project** | `patron/data/projects/<uid>.json` | + `owner`, `owner_email` |
| **GraphRecord** (deployed) | `agent_runtime/data/graphs/<uid>.json` | + `owner` (stamped at deploy) |
| **agent record** (legacy/flat) | `agent_runtime/data/agents/<uid>.json` | + `owner` |
| **resource item** | resource store | + `owner` (or keep global/shared — decide per resource) |
| **scheduler binding** | Redis hash (agent_scheduler) | + `owner` in `event_data`, OR derive via record_uid |
| **ingress binding** | folder_watch/http_ingress/stt_ingress store | + `owner`, OR derive via record_uid |

**Derivation shortcut:** scheduler/ingress bindings all carry `record_uid`. Rather than teach every
firing service about owners, they can stay owner-agnostic: the **runtime** owns the authorization
(it holds the `GraphRecord.owner`), and firing services simply fire by `record_uid` as today. A fire
event still runs only the graph it routes to; the *observability/control* surface (where a human
acts) is the runtime + Patron, which is where authz lives. **Recommended: bindings inherit ownership
via `record_uid`; do not duplicate `owner` into every service** (less surface, one source of truth).

**Migration / backfill:** existing records get `owner = <current single user's sub>` (the
`logus2k@gmail.com` principal) so nothing is orphaned on rollout.

---

## 5. Authorization — the single enforcement layer

One authz seam, applied uniformly. Two places (because there are two servers):

### 5.1 agent_runtime (FastAPI) — a dependency
A `require_owner(uid)` dependency (and a `principal` dependency) on the admin router:
- extract the principal from `X-Patron-User` (401 if absent),
- for any `/{uid}` route: load the entity, `403` unless `entity.owner == principal`,
- for list routes: filter to `owner == principal`,
- for create routes: stamp `owner = principal`.

### 5.2 Patron serve.py — mirror + scope
- derive principal from `X-Auth-Request-Email`/`-User`, set `X-Patron-User` on every server-to-server
  call to the farm,
- project store: `GET /api/projects` lists only the caller's; `GET/PUT/DELETE /api/projects/<uid>`
  and the deploy/fire/events proxies check ownership (or rely on the farm's 403 and pass it through),
- write `owner` on project create.

### 5.3 Endpoint authorization matrix (the full surface)

| Endpoint (agent_runtime) | Rule |
|---|---|
| `GET /health`, `GET /composer/catalog`, `POST /composer/compile` | **public** (no owned data) |
| `GET /resources/catalog`, `/resources/{rid}` … | **public** or **owner-scoped** per resource kind (decide §9) |
| `GET /admin/agents` | **scope to owner** |
| `GET/PUT/DELETE /admin/agents/{uid}`, `…/enable`, `…/disable` | **owner-check** |
| `POST /admin/agents` | stamp owner |
| `GET /admin/runs` | **scope to owner** (filter by owned record_uids) |
| `GET /admin/consistency` | **admin-only** (superuser allow-list, §9.4) |
| `GET /admin/channels/*`, `POST /admin/tools/template-writer` | **public-ish** (no cross-tenant data) — but rate-limit |
| `POST /admin/projects/{uid}/deploy` / `/undeploy` / `/fire` | **owner-check** (deploy stamps owner on first create) |
| `GET /admin/projects/{uid}/events` (Trace SSE) | **owner-check** ← the Trace panel inherits this for free |
| `GET /admin/projects`, `GET /admin/projects/{uid}`, `DELETE …` | **scope / owner-check** |

| Endpoint (Patron serve.py) | Rule |
|---|---|
| `GET /api/projects` | **scope to owner** |
| `GET/PUT/DELETE /api/projects/{uid}` | **owner-check** |
| `POST /api/projects/{uid}/deploy` `/fire`, `GET …/events`, `POST /api/undeploy/{uid}` | forward `X-Patron-User`; farm enforces |
| `GET/PUT /api/workspace` | **DROP** — removed with its store (auto-save gone; boot no longer reads it) |

Note: `GET/PUT /api/workspace` is a dead **single global file** (auto-save was removed; boot no longer
reads it) — it is DROPPED entirely (§9.3), not made per-principal.

---

## 6. Runtime & bus isolation

- **Farm: no change.** One process, dispatch by `record_uid`, each run a bounded task with its own
  `cid`. A user's run cannot reach another's graph (routing is by `record_uid`, which the user owns).
- **Bus (Valkey): shared internal infra.** Run events go to the runs stream keyed by `cid`; the
  `EventHub` fan-out is in-process. Cross-tenant leakage is prevented at the **`/events` authz**, not
  at the bus — the bus is not user-facing.
- **Firing services (scheduler/ingress): owner-agnostic** (per §4 recommendation) — they fire by
  `record_uid`; the human-facing authz is upstream.

---

## 7. Implementation plan (phased — build in this order, verify each)

**Phase 0 — Identity plumbing (no behavior change).**
Un-pin OAuth2Proxy to admit multiple principals (still domain-gated). Add `principal()` on serve.py
(from `X-Auth-Request-*`) and forward `X-Patron-User` to the farm; add `principal()` on the farm
(from `X-Patron-User`, trusted only from the internal origin). 401 on missing principal.
*Verify:* two different logins reach the app; the farm logs the correct principal per request.

**Phase 1 — Ownership data model + backfill.**
Add `owner`/`owner_email` to project docs + `GraphRecord` (+ agent records). Stamp on create/deploy.
Backfill all existing records to the current single user. *Verify:* new project → owner set; existing
projects → owner backfilled; deploy stamps `GraphRecord.owner`.

**Phase 2 — Authorization layer.**
The `require_owner` dependency (farm) + serve.py scoping/forwarding per the §5.3 matrix. Deny
cross-owner with 403; scope all list endpoints; drop the dead `/api/workspace`. *Verify (the key test):*
principal A cannot GET/deploy/fire/events/delete principal B's project (403), cannot see it in the
list, and B's `/events` stream never receives A's payloads.

**Phase 3 — Patron UX.**
Open-Project list already scoped by the backend; show owner where useful; friendly 403 handling.
*Verify:* live, two browser sessions (two principals) see only their own projects.

**Phase 4 — THEN the Trace panel (plan A).**
Build the live Trace/Debug panel (payload-on-edges → SSE → panel). Because `/events` is already
owner-checked in Phase 2, the trace is tenant-safe from day one.

**Later (not v1):** sharing/collaboration (§8), teams/orgs, per-tenant quotas & rate-limits, audit log.

---

## 8. Sharing & teams (future, designed-for not built)

Ownership is the v1 primitive. Collaboration layers on top without changing it:
- **Share a project** = an ACL entry `{uid, principal, role}` (viewer/editor). The authz check
  becomes `owner == caller OR caller ∈ acl(uid)`.
- **Teams/orgs** = a principal can belong to groups; ownership/ACL can name a group.
- **Transfer ownership** = an explicit, audited action (the only way `owner` changes).

Design the Phase-2 authz check as `can_access(principal, uid, action)` (not a bare `owner ==`) so
these slot in without touching call sites.

---

## 9. Decisions (resolved)

1. **Principal key = OIDC `sub`.** ✅ Owner is keyed on the immutable `sub`; store `owner_email`
   alongside for display. Confirm at build time that OAuth2Proxy exposes `sub` (via
   `X-Auth-Request-User`); if not, resolve it from the userinfo/JWT the proxy forwards.
2. **Resources = global / shared.** ✅ Presets/personas/MCP-tools/skills are platform capabilities
   everyone uses — their endpoints stay **public** (no owner). Only *projects / agents / runs* are
   owned.
3. **`/api/workspace` = drop it.** ✅ Auto-save is already removed and boot no longer reads the
   workspace, so the single global workspace file is dead weight — remove the endpoint + its store
   (verify nothing still reads/writes it before deleting). No per-principal workspace is introduced.
4. **Admin/superuser role = YES (allow-list).** ✅ An env/config allow-list of admin `sub`s bypasses
   the ownership check: admins can list-all and hit operational endpoints (`/admin/consistency`,
   support). Normal users stay strictly scoped. `can_access()` returns true for admins.
5. **Firing services = owner-agnostic.** ✅ Scheduler / folder_watch / http_ingress / stt_ingress do
   **not** store or enforce `owner`. A binding can only be created during a deploy that is already
   owner-checked, so it can only exist for a project you own; firing it just runs that graph.
   Ownership is enforced only where a human acts/observes (Patron + runtime admin API). *(Separate,
   non-tenancy concern: who may hit a Web Initiator's HTTP route is edge-auth on that route.)*

---

## 10. Testing

- **Authz unit tests** (farm): owner match → 200; mismatch → 403; missing principal → 401; list
  scoping returns only owned; create stamps owner.
- **Migration test:** backfill assigns the current user; no orphans.
- **Cross-tenant integration test (the acceptance test):** principal A and principal B; assert B
  cannot read/deploy/fire/observe/delete A's project through **any** endpoint, and B's `/events`
  never yields A's payloads.
- **Header-trust test:** a client-supplied `X-Patron-User` to the farm from a non-proxy origin is
  rejected (defense against header spoofing).
