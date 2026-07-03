"""Deploy lifecycle — a Patron Project → one live GraphRecord + its firing binding.

Phase 05 (§9.3, §9.3.1, §9.4). A **Project deploys 1:1 to one graph record** in the
``GraphRegistry`` (idempotent by the Project ``uid``, version-bumped on re-deploy), and
— if the composition's initiator is a **schedule-type Trigger** — a scheduler **Schedule
+ Binding** is upserted so that schedule's firing IS the Project's firing (§9.3.1). The
firing binding's ``event_data`` carries ``record_uid = <project uid>`` so the farm can
route a fired event to the deployed graph record.

Undeploy removes the graph record AND its firing binding; source assets (the schedule
itself, agent profiles, destinations) are left intact and reusable (§9.4).

The scheduler base URL is config (``settings.scheduler_url``). The ``SchedulerClient`` is
a thin async HTTP wrapper; tests inject a fake so no live scheduler is needed. Errors are
surfaced loudly — a scheduler failure during Deploy is reported (not swallowed), but does
NOT roll back the graph-record upsert (the record is the Project's own state; the binding
is best-effort glue whose failure is returned as a warning so the user can retry).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import httpx

from .composer.lower import lower_project
from .config import settings
from .dsl_graph import GraphRecord
from .graph_registry import GraphRegistry

log = logging.getLogger("agent_runtime.deploy")

# A deployed Project's derived schedule/binding ids (deterministic from the uid so a
# re-deploy targets the SAME scheduler entities — idempotent, no duplicates). The
# scheduler requires ids matching ^[A-Za-z0-9._:-]+$, so the uid is sanitized.
_ID_SAFE = re.compile(r"[^A-Za-z0-9._:-]")


def _sanitize_id(uid: str) -> str:
    return _ID_SAFE.sub("-", uid)


def schedule_id_for(uid: str) -> str:
    return f"proj-{_sanitize_id(uid)}"


def binding_id_for(uid: str) -> str:
    return f"proj-{_sanitize_id(uid)}-fire"


class SchedulerClient:
    """Thin async client for the agent_scheduler Schedule + Bindings API (§6, Phase 03).

    Idempotent helpers: ``upsert_schedule`` creates or PATCHes a schedule; ``upsert_binding``
    creates or PATCHes a binding. Only the verbs Deploy/Undeploy need are wrapped."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        timeout_s: Optional[float] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._base = (base_url or settings.scheduler_url).rstrip("/")
        self._timeout = timeout_s if timeout_s is not None else settings.scheduler_timeout_s
        # A transport seam so a test can mock the scheduler HTTP without a live service.
        self._transport = transport

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base, timeout=self._timeout, transport=self._transport
        )

    async def upsert_schedule(
        self, schedule_id: str, *, trigger_type: str = "cron",
        trigger_args: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Create the schedule, or PATCH it if it already exists (idempotent re-deploy).

        ``trigger_type`` is one of the scheduler's native kinds (``cron`` / ``interval`` /
        ``date``); ``trigger_args`` is the matching arg set (cron_expression+timezone,
        interval parts, or run_date). Passed through verbatim to agent_scheduler."""
        args: dict[str, Any] = dict(trigger_args or {})
        body = {"schedule_id": schedule_id, "trigger_type": trigger_type, "trigger_args": args}
        async with await self._client() as client:
            resp = await client.post("/schedules", json=body)
            if resp.status_code == 409:
                # Already exists → update its trigger in place (PATCH needs both together).
                resp = await client.patch(
                    f"/schedules/{schedule_id}",
                    json={"trigger_type": trigger_type, "trigger_args": args},
                )
            resp.raise_for_status()
            return resp.json()

    async def upsert_binding(
        self,
        schedule_id: str,
        binding_id: str,
        *,
        target_stream_id: str,
        event_type: str,
        event_data: dict[str, Any],
        room: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create the firing binding, or PATCH it if it already exists (idempotent)."""
        body: dict[str, Any] = {
            "binding_id": binding_id,
            "target_stream_id": target_stream_id,
            "event_type": event_type,
            "event_data": event_data,
        }
        if room is not None:
            body["room"] = room
        async with await self._client() as client:
            resp = await client.post(f"/schedules/{schedule_id}/bindings", json=body)
            if resp.status_code == 409:
                resp = await client.patch(
                    f"/schedules/{schedule_id}/bindings/{binding_id}",
                    json={
                        "target_stream_id": target_stream_id,
                        "event_type": event_type,
                        "event_data": event_data,
                        **({"room": room} if room is not None else {}),
                    },
                )
            resp.raise_for_status()
            return resp.json()

    async def delete_binding(self, schedule_id: str, binding_id: str) -> bool:
        """Remove the firing binding. Returns True if it existed, False on 404."""
        async with await self._client() as client:
            resp = await client.delete(f"/schedules/{schedule_id}/bindings/{binding_id}")
            if resp.status_code == 404:
                return False
            resp.raise_for_status()
            return True

    async def count_bindings(self, schedule_id: str) -> Optional[int]:
        """How many bindings the schedule still has, or None if the schedule is gone
        (404). Used by undeploy to decide whether the derived schedule is now empty."""
        async with await self._client() as client:
            resp = await client.get(f"/schedules/{schedule_id}/bindings")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            return len(data) if isinstance(data, list) else 0

    async def delete_schedule(self, schedule_id: str) -> bool:
        """Remove the derived ``proj-<uid>`` schedule. Returns True if it existed,
        False on 404 (idempotent)."""
        async with await self._client() as client:
            resp = await client.delete(f"/schedules/{schedule_id}")
            if resp.status_code == 404:
                return False
            resp.raise_for_status()
            return True


# --- Non-schedule initiators (File / Web / STT) ------------------------------
# Each maps a Patron initiator block to its backing service's /bindings API. Deploy
# creates the binding (record_uid = project uid); Undeploy removes it. The three APIs
# differ (paths, id field, list shape), captured declaratively here.
def _split_patterns(raw: Any) -> list[str]:
    """"*.pdf, *.txt" (Patron's free-text patterns field) -> ["*.pdf", "*.txt"]."""
    if isinstance(raw, list):
        return [str(p).strip() for p in raw if str(p).strip()]
    return [p.strip() for p in re.split(r"[,\s]+", str(raw or "").strip()) if p.strip()]


# comp node "type" -> service binding spec. ``payload`` builds the create body from the
# block's Patron properties; ``id_field`` is what DELETE keys on; ``list_unwrap`` names the
# envelope key when the list endpoint wraps its array (stt), else the response IS the list.
_INITIATOR_SPECS: dict[str, dict[str, Any]] = {
    "file_initiator": {
        "service": "folder_watch",
        "create_path": "/bindings",
        "list_path": "/bindings",
        "del_path": "/bindings/{id}",
        "id_field": "binding_id",
        "list_unwrap": None,
        "payload": lambda uid, p: {
            "record_uid": uid,
            "path": str(p.get("watch_path") or "").strip(),
            "patterns": _split_patterns(p.get("patterns")),
            "name": (str(p.get("name")).strip() or None) if p.get("name") else None,
        },
    },
    "web_initiator": {
        "service": "http_ingress",
        "create_path": "/admin/bindings",
        "list_path": "/admin/bindings",
        "del_path": "/admin/bindings/{id}",
        "id_field": "binding_id",
        "list_unwrap": None,
        "payload": lambda uid, p: {
            "record_uid": uid,
            "route": str(p.get("route") or "").strip(),
            "method": (str(p.get("method") or "POST").strip().upper()),
        },
    },
    "stt_initiator": {
        "service": "stt_ingress",
        "create_path": "/bindings",
        "list_path": "/bindings",
        "del_path": "/bindings/{id}",     # stt keys DELETE by source_id
        "id_field": "source_id",
        "list_unwrap": "bindings",
        "payload": lambda uid, p: {
            "source_id": str(p.get("source") or "").strip(),
            "record_uid": uid,
        },
    },
}


class IngressClient:
    """Thin async client for the File/Web/STT initiator services' ``/bindings`` APIs.

    ``bind`` is idempotent: it first deletes any existing binding for this ``record_uid``
    (so a re-deploy with a changed path/route/source doesn't leave a stale one), then
    creates the new binding. ``unbind_all`` removes this record's binding wherever it
    lives (a Project has one initiator, but at undeploy we don't have the composition, so
    we clear across all three — best-effort, each service independent). Tests inject a
    fake via the same method surface; a transport seam mocks HTTP without live services."""

    def __init__(
        self,
        *,
        urls: Optional[dict[str, str]] = None,
        timeout_s: Optional[float] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        u = urls or {}
        self._urls = {
            "folder_watch": (u.get("folder_watch") or settings.folder_watch_url).rstrip("/"),
            "http_ingress": (u.get("http_ingress") or settings.http_ingress_url).rstrip("/"),
            "stt_ingress": (u.get("stt_ingress") or settings.stt_ingress_url).rstrip("/"),
        }
        self._timeout = timeout_s if timeout_s is not None else settings.ingress_timeout_s
        self._transport = transport

    def _client(self, service: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._urls[service], timeout=self._timeout, transport=self._transport
        )

    async def _list(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        async with self._client(spec["service"]) as client:
            resp = await client.get(spec["list_path"])
            resp.raise_for_status()
            data = resp.json()
        if spec["list_unwrap"]:
            data = (data or {}).get(spec["list_unwrap"], [])
        return data if isinstance(data, list) else []

    async def _delete_for_record(self, spec: dict[str, Any], record_uid: str) -> int:
        removed = 0
        for b in await self._list(spec):
            if b.get("record_uid") == record_uid:
                async with self._client(spec["service"]) as client:
                    resp = await client.delete(spec["del_path"].format(id=b.get(spec["id_field"])))
                    if resp.status_code not in (200, 204, 404):
                        resp.raise_for_status()
                    removed += 1
        return removed

    async def bind(self, kind: str, record_uid: str, props: dict[str, Any]) -> dict[str, Any]:
        spec = _INITIATOR_SPECS[kind]
        await self._delete_for_record(spec, record_uid)  # idempotent re-deploy
        payload = spec["payload"](record_uid, props)
        async with self._client(spec["service"]) as client:
            resp = await client.post(spec["create_path"], json=payload)
            resp.raise_for_status()
        return {"service": spec["service"], "payload": payload}

    async def unbind_all(self, record_uid: str) -> tuple[int, list[str]]:
        """Remove this record's binding from every initiator service. Best-effort: a
        service that errors/is-down yields a warning, never aborts the others."""
        removed = 0
        warnings: list[str] = []
        for kind, spec in _INITIATOR_SPECS.items():
            try:
                removed += await self._delete_for_record(spec, record_uid)
            except httpx.HTTPError as exc:
                warnings.append(f"{spec['service']} unbind failed: {exc}")
        return removed, warnings


def _find_initiator(composition: dict[str, Any]) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """The FIRST non-schedule initiator (File/Web/STT) node + its kind, or (None, None).
    A schedule Trigger is handled separately by ``_find_schedule_initiator``."""
    for n in (composition or {}).get("nodes") or []:
        if n.get("type") in _INITIATOR_SPECS:
            return n.get("type"), n
    return None, None


def _find_schedule_initiator(composition: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The FIRST Trigger node in the composition, or None. Its firing becomes the
    Project's firing (§9.3.1). Every Trigger is a schedule now (cron/interval/date);
    a legacy ``channel`` trigger — a type we removed — is skipped (it never fired)."""
    for n in (composition or {}).get("nodes") or []:
        if n.get("type") != "trigger":
            continue
        props = n.get("properties") or {}
        if str(props.get("trigger_type") or "") == "channel":
            continue  # legacy channel node: no firing binding (matches old behaviour)
        return n
    return None


def _build_trigger(props: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Map a Trigger node's ``schedule_mode`` + fields to the scheduler's
    ``(trigger_type, trigger_args)``. Mirrors ``blocks.Trigger.schedule_spec`` but reads
    the raw node properties (deploy works off the serialized composition, not the block)."""
    mode = str(props.get("schedule_mode") or "cron").strip()
    if mode == "interval":
        unit = str(props.get("interval_unit") or "minutes").strip()
        try:
            value = int(props.get("interval_value") or 0)
        except (TypeError, ValueError):
            value = 0
        return "interval", {unit: value}
    if mode == "date":
        return "date", {"run_date": str(props.get("run_date") or "").strip()}
    args: dict[str, Any] = {"cron_expression": str(props.get("cron") or "0 7 * * *").strip()}
    tz = str(props.get("timezone") or "").strip()
    if tz:
        args["timezone"] = tz
    return "cron", args


async def deploy_project(
    *,
    uid: str,
    name: str,
    composition: dict[str, Any],
    registry: GraphRegistry,
    scheduler: SchedulerClient,
    ingress: Optional["IngressClient"] = None,
    farm_stream_id: Optional[str] = None,
) -> dict[str, Any]:
    """Deploy a Project: lower → upsert one GraphRecord (idempotent, version-bumped) →
    establish the firing binding for whichever initiator the composition has:
      * schedule Trigger  -> agent_scheduler Schedule + Binding (§9.3.1);
      * File/Web/STT       -> the matching ingress service's /bindings (record_uid).

    Returns ``{uid, version, warnings, firing}``. ``warnings`` is the advisory validation
    list (never blocks). ``firing`` describes the binding outcome (or why none was made). A
    binding error is appended to ``warnings`` and does NOT undo the stored graph record."""
    record, warnings = lower_project(uid, name, composition)
    stored: GraphRecord = registry.upsert(record)  # idempotent; bumps version on re-deploy

    firing: dict[str, Any] = {"bound": False, "reason": None}
    initiator = _find_schedule_initiator(composition)
    ing_kind, ing_node = _find_initiator(composition)
    if initiator is None and ing_kind is not None:
        # File/Web/STT initiator → bind to its backing service (mirror of the scheduler).
        ingress = ingress or IngressClient()
        spec = _INITIATOR_SPECS[ing_kind]
        try:
            bound = await ingress.bind(ing_kind, uid, ing_node.get("properties") or {})
            firing = {
                "bound": True,
                "service": spec["service"],
                "initiator": ing_kind,
                "binding": bound.get("payload"),
                "reason": None,
            }
            log.info("deploy %s '%s': %s binding -> record_uid=%s (%s)",
                     uid, name, spec["service"], uid, bound.get("payload"))
        except httpx.HTTPError as exc:
            msg = f"{spec['service']} binding failed: {exc}"
            log.error("deploy %s: %s", uid, msg)
            firing = {"bound": False, "service": spec["service"], "reason": msg}
            warnings = list(warnings) + [msg]
    elif initiator is None:
        firing["reason"] = "no schedule/File/Web/STT initiator; nothing to bind"
    else:
        props = initiator.get("properties") or {}
        trigger_type, trigger_args = _build_trigger(props)
        task = str(props.get("task") or "").strip()
        sched_id = schedule_id_for(uid)
        bind_id = binding_id_for(uid)
        stream = farm_stream_id or settings.farm_stream_id
        # The binding's event_data identifies THIS graph record so the farm routes a fired
        # event to it (record_uid). agent_name aids logs/back-compat. ``task`` is the
        # schedule's SEED per the firing contract (data.task) — a fixed query/message a
        # scheduled agent starts from (feeds RAG-pre + the Agent's {input}); "" if none.
        event_data = {"record_uid": uid, "agent_name": name}
        if task:
            event_data["task"] = task
        try:
            await scheduler.upsert_schedule(
                sched_id, trigger_type=trigger_type, trigger_args=trigger_args,
            )
            await scheduler.upsert_binding(
                sched_id,
                bind_id,
                target_stream_id=stream,
                event_type="schedule.fired",
                event_data=event_data,
            )
            firing = {
                "bound": True,
                "schedule_id": sched_id,
                "binding_id": bind_id,
                "trigger_type": trigger_type,
                "trigger_args": trigger_args,
                "target_stream_id": stream,
                "reason": None,
            }
            log.info(
                "deploy %s '%s': firing binding %s -> record_uid=%s (schedule %s, %s %r)",
                uid, name, bind_id, uid, sched_id, trigger_type, trigger_args,
            )
        except httpx.HTTPError as exc:
            msg = f"scheduler firing binding failed: {exc}"
            log.error("deploy %s: %s", uid, msg)
            firing = {"bound": False, "schedule_id": sched_id, "reason": msg}
            warnings = list(warnings) + [msg]

    return {
        "uid": stored.uid,
        "version": stored.version,
        "warnings": warnings,
        "firing": firing,
    }


async def undeploy_project(
    *,
    uid: str,
    registry: GraphRegistry,
    scheduler: SchedulerClient,
    ingress: Optional["IngressClient"] = None,
) -> dict[str, Any]:
    """Undeploy: remove the live GraphRecord AND its firing binding (§9.4) — for a schedule
    Project, the scheduler binding (+ the DERIVED ``proj-<uid>`` schedule once it has no
    bindings left, since Deploy created it); for a File/Web/STT Project, its ingress
    binding. We don't have the composition here, so we clear the ingress binding across all
    three services by ``record_uid`` (a Project has one, so this removes it wherever it is).
    Author-owned source assets (agent profiles, destinations) are always left intact.
    Idempotent — a missing record/binding/schedule is reported, not an error.

    Returns ``{uid, removed, firing_removed, schedule_removed, ingress_removed, warnings}``."""
    removed = registry.delete(uid)
    warnings: list[str] = []
    sched_id = schedule_id_for(uid)
    bind_id = binding_id_for(uid)
    firing_removed = False
    schedule_removed = False
    ingress_removed = 0
    try:
        firing_removed = await scheduler.delete_binding(sched_id, bind_id)
        # Clean up the derived schedule iff it's now empty (never delete one that still
        # carries other bindings the user may have added).
        remaining = await scheduler.count_bindings(sched_id)
        if remaining == 0:
            schedule_removed = await scheduler.delete_schedule(sched_id)
    except httpx.HTTPError as exc:
        msg = f"scheduler cleanup failed: {exc}"
        log.error("undeploy %s: %s", uid, msg)
        warnings.append(msg)
    # File/Web/STT binding cleanup (best-effort across the three services).
    ingress = ingress or IngressClient()
    ingress_removed, ing_warnings = await ingress.unbind_all(uid)
    warnings.extend(ing_warnings)
    if not removed:
        warnings.append(f"no live graph record for uid '{uid}' (already undeployed?)")
    log.info(
        "undeploy %s: record removed=%s, firing removed=%s, schedule removed=%s, ingress removed=%d",
        uid, removed, firing_removed, schedule_removed, ingress_removed,
    )
    return {
        "uid": uid,
        "removed": removed,
        "firing_removed": firing_removed,
        "schedule_removed": schedule_removed,
        "ingress_removed": ingress_removed,
        "warnings": warnings,
    }
