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

# TODO(Step 2): install the bus SDK — agent-bus-client[bus] (canonical envelope +
# the glide BusClient consumer/producer). It's a sibling-repo package; wire its
# build context here (an extra build context or a published wheel) when the farm
# consumer is implemented. Step 0 only needs FastAPI/uvicorn for /health.

# Application source (package lives under src/agent_runtime).
COPY src/ ./src/
ENV PYTHONPATH=/app/src

# Agent records + design docs.
COPY data/ ./data/
COPY documents/ ./documents/

# Single-process entrypoint: uvicorn serving /health (and, later, the farm).
CMD ["python", "-m", "agent_runtime.app"]
