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
        self, schedule_id: str, *, cron: str, timezone: str = ""
    ) -> dict[str, Any]:
        """Create the schedule, or PATCH it if it already exists (idempotent re-deploy)."""
        args: dict[str, Any] = {"cron_expression": cron}
        if timezone:
            args["timezone"] = timezone
        body = {"schedule_id": schedule_id, "trigger_type": "cron", "trigger_args": args}
        async with await self._client() as client:
            resp = await client.post("/schedules", json=body)
            if resp.status_code == 409:
                # Already exists → update its cron/timezone in place.
                resp = await client.patch(
                    f"/schedules/{schedule_id}",
                    json={"trigger_type": "cron", "trigger_args": args},
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


def _find_schedule_initiator(composition: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The FIRST schedule-type Trigger node in the composition, or None. Its firing
    becomes the Project's firing (§9.3.1). Non-schedule triggers (channel type) fire on
    the channel event, not a schedule, so they establish no scheduler binding here."""
    for n in (composition or {}).get("nodes") or []:
        if n.get("type") != "trigger":
            continue
        props = n.get("properties") or {}
        if (props.get("trigger_type") or "schedule") == "schedule":
            return n
    return None


async def deploy_project(
    *,
    uid: str,
    name: str,
    composition: dict[str, Any],
    registry: GraphRegistry,
    scheduler: SchedulerClient,
    farm_stream_id: Optional[str] = None,
) -> dict[str, Any]:
    """Deploy a Project: lower → upsert one GraphRecord (idempotent, version-bumped) →
    establish the firing binding for a schedule-type Trigger initiator.

    Returns ``{uid, version, warnings, firing}``. ``warnings`` is the advisory validation
    list (never blocks). ``firing`` describes the scheduler binding outcome (or why none
    was created). A scheduler error is appended to ``warnings`` and does NOT undo the
    already-stored graph record."""
    record, warnings = lower_project(uid, name, composition)
    stored: GraphRecord = registry.upsert(record)  # idempotent; bumps version on re-deploy

    firing: dict[str, Any] = {"bound": False, "reason": None}
    initiator = _find_schedule_initiator(composition)
    if initiator is None:
        firing["reason"] = "no schedule-type Trigger initiator; nothing to bind"
    else:
        props = initiator.get("properties") or {}
        cron = str(props.get("cron") or "0 7 * * *").strip()
        timezone = str(props.get("timezone") or "").strip()
        sched_id = schedule_id_for(uid)
        bind_id = binding_id_for(uid)
        stream = farm_stream_id or settings.farm_stream_id
        # The binding's event_data identifies THIS graph record so the farm routes a
        # fired event to it (record_uid). agent_name aids logs/back-compat.
        event_data = {"record_uid": uid, "agent_name": name}
        try:
            await scheduler.upsert_schedule(sched_id, cron=cron, timezone=timezone)
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
                "cron": cron,
                "timezone": timezone or None,
                "target_stream_id": stream,
                "reason": None,
            }
            log.info(
                "deploy %s '%s': firing binding %s -> record_uid=%s (schedule %s, cron %r)",
                uid, name, bind_id, uid, sched_id, cron,
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
) -> dict[str, Any]:
    """Undeploy: remove the live GraphRecord AND its firing binding (§9.4). The DERIVED
    ``proj-<uid>`` schedule is also removed **once it has no bindings left** — it was
    created by Deploy, so leaving an empty shell behind is orphaned clutter. If the user
    added their own extra bindings to it, the schedule is kept (it still has bindings).
    Author-owned source assets (agent profiles, destinations) are always left intact.
    Idempotent — a missing record/binding/schedule is reported, not an error.

    Returns ``{uid, removed, firing_removed, schedule_removed, warnings}``."""
    removed = registry.delete(uid)
    warnings: list[str] = []
    sched_id = schedule_id_for(uid)
    bind_id = binding_id_for(uid)
    firing_removed = False
    schedule_removed = False
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
    if not removed:
        warnings.append(f"no live graph record for uid '{uid}' (already undeployed?)")
    log.info(
        "undeploy %s: record removed=%s, firing binding removed=%s, schedule removed=%s",
        uid, removed, firing_removed, schedule_removed,
    )
    return {
        "uid": uid,
        "removed": removed,
        "firing_removed": firing_removed,
        "schedule_removed": schedule_removed,
        "warnings": warnings,
    }
