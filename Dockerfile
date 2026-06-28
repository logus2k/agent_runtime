# agent_runtime — single async process (FastAPI /health + the agent "farm").
#
# IMPORTANT: the base image MUST be glibc (Debian slim), NOT alpine/musl.
# valkey-glide (the bus client's Rust core) ships no musl wheels, so an Alpine
# base would fail to install/run the bus client.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first for layer caching.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# The bus SDK (canonical envelope + glide BusClient consumer/producer) comes from
# the sibling agent_bus repo, supplied as a named build context by docker-compose
# (additional_contexts.busclient_sdk = ../agent_bus/sdk/python). Its deps
# (pydantic/socketio/aiohttp/valkey-glide) are already in requirements.txt.
COPY --from=busclient_sdk . /opt/busclient_sdk
RUN pip install '/opt/busclient_sdk[bus]'

# Application source (package lives under src/agent_runtime).
COPY src/ ./src/
ENV PYTHONPATH=/app/src

# Agent records + design docs.
COPY data/ ./data/
COPY documents/ ./documents/

# Single-process entrypoint: uvicorn serving /health (and, later, the farm).
CMD ["python", "-m", "agent_runtime.app"]
