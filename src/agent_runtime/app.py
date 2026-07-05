"""agent_runtime — process entrypoint.

One lean async process: a FastAPI app exposing ``/health``, whose lifespan will
(Step 2+) own the bus-consumer "farm" — the loop that activates dormant agent
records into transient bounded tasks on each trigger event, plus the reaper that
reclaims abandoned jobs. Step 0 ships the skeleton + health only; the farm is
wired in once the bus consumer lands.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .admin import router as admin_router
from .composer_api import router as composer_router
from .resources_api import router as resources_router
from .config import settings
from .farm import Farm
from .graph_registry import GraphRegistry
from .registry import Registry
from .runner import Runner

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("agent_runtime")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load the agent records, connect the bus, then start the farm with the runner
    # as its pipeline handler. The farm owns the consumer loop + reaper; the runner
    # runs each triggered agent (brain → guardrail → delivery) and emits run events.
    log.info("agent_runtime %s starting", __version__)
    registry = Registry(settings.agents_dir)
    registry.load_all()

    # The graph-record registry (deployed Projects, §9.3) — shared with the admin API's
    # /admin/projects/* deploy lifecycle AND with the farm, so a deployed Project is live
    # and firable at once. Created before the farm starts so its routing is wired.
    # Persisted under settings.graphs_dir → deployed Projects survive a restart (§9.3).
    graph_registry = GraphRegistry(settings.graphs_dir)

    farm = Farm(settings, registry, graph_registry=graph_registry)
    await farm.connect()
    runner = Runner(settings, farm.bus)
    farm.set_handler(runner.run)
    # Route fired events carrying event_data.record_uid to the deployed GraphRecord (§9.3).
    farm.set_graph_routing(graph_registry, runner.run_graph_record)
    await farm.start()
    app.state.farm = farm
    # Shared with the admin API (deploy/reload) so a pushed record is live at once.
    app.state.registry = registry
    app.state.agents_dir = settings.agents_dir
    app.state.graph_registry = graph_registry
    try:
        yield
    finally:
        log.info("agent_runtime stopping")
        await farm.stop()


app = FastAPI(title="agent_runtime", version=__version__, lifespan=lifespan)
app.include_router(admin_router)
app.include_router(composer_router)
app.include_router(resources_router)


@app.get("/health")
async def health(request: Request) -> Response:
    """Liveness + readiness probe (used by the compose healthcheck).

    Reports ``degraded`` (HTTP 503) when the farm's consume loop can't read the stream — e.g.
    the consumer group vanished AND couldn't be recreated (bus down). That turns a farm that is
    "up" but silently not consuming into a FAILING healthcheck, so an orchestrator/monitor sees
    it instead of the failure hiding behind a green container."""
    farm = getattr(request.app.state, "farm", None)
    consuming = farm.consuming() if farm is not None and hasattr(farm, "consuming") else True
    body = {
        "status": "ok" if consuming else "degraded",
        "service": "agent_runtime",
        "version": __version__,
        "consuming": consuming,
    }
    return JSONResponse(body, status_code=200 if consuming else 503)


# Static admin UI at the app root. Mounted LAST so the explicit /health and /admin
# routes above take precedence; the SPA assets and index.html are served for the rest.
# Guarded by existence so a bare/dev process without a frontend dir still boots.
_frontend = Path(settings.frontend_dir)
if _frontend.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")
    log.info("serving admin UI from %s", _frontend.resolve())
else:
    log.warning("frontend dir %s not found — admin UI not served", _frontend)


def main() -> None:
    uvicorn.run(
        "agent_runtime.app:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
