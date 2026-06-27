"""Environment-driven configuration.

A single immutable ``Settings`` instance, populated from the environment (with a
``.env`` loaded in dev). Every knob has a safe default so the app runs with an
empty environment. Keep ALL tunables here — no magic numbers scattered across the
codebase. Mirrors agent_bus / agent_scheduler config conventions.

Stream/Valkey defaults MUST match agent_bus so the farm consumes the same streams
the scheduler writes to. Secrets (e.g. the WhatsApp token) come from the
environment, never from a DSL agent record.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load .env if present (no-op in containers that inject real env vars).
load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else raw


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # --- Valkey connection (shared valkey-bus) ---
    valkey_host: str = _str("VALKEY_HOST", "127.0.0.1")
    valkey_port: int = _int("VALKEY_PORT", 6379)

    # --- Stream conventions (MUST match agent_bus) ---
    stream_prefix: str = _str("STREAM_PREFIX", "stream:")
    dlq_stream: str = _str("DLQ_STREAM", "stream:dlq")
    stream_ttl_s: int = _int("STREAM_TTL_S", 3600)

    # --- Farm ingress (the shared stream the scheduler targets) ---
    # bare id; the full key is ``stream:<farm_stream_id>``. Scheduler jobs set
    # target_stream_id = this, with event_data { agent: <id> }.
    farm_stream_id: str = _str("FARM_STREAM_ID", "agent-runtime")
    consumer_group: str = _str("CONSUMER_GROUP", "cg:agent-runtime")
    consumer_name: str = _str("CONSUMER_NAME", "farm")  # make_consumer() adds host+pid

    # --- Consumer loop tuning ---
    read_count: int = _int("READ_COUNT", 32)
    read_block_ms: int = _int("READ_BLOCK_MS", 2000)
    reaper_interval_s: int = _int("REAPER_INTERVAL_S", 15)
    reaper_min_idle_ms: int = _int("REAPER_MIN_IDLE_MS", 30000)

    # --- Dispatch bounds (never oversubscribe the shared brain) ---
    max_concurrency: int = _int("MAX_CONCURRENCY", 4)  # ~ agent_server slot count
    job_timeout_s: int = _int("JOB_TIMEOUT_S", 120)

    # --- Idempotency (at-least-once delivery → dedupe on cid+sid) ---
    dedupe_ttl_s: int = _int("DEDUPE_TTL_S", 3600)

    # --- Agent registry ---
    agents_dir: str = _str("AGENTS_DIR", "data/agents")

    # --- Downstream services (reachable by service name on logus2k_network) ---
    agent_server_url: str = _str("AGENT_SERVER_URL", "http://agent_server:7701")
    noted_mcp_url: str = _str("NOTED_MCP_URL", "http://noted:8123/mcp/")
    # noted advertises raw tool names; the client namespaces them as noted__<raw>.
    mcp_tool_prefix: str = _str("MCP_TOOL_PREFIX", "noted__")
    whatsapp_bridge_url: str = _str("WHATSAPP_BRIDGE_URL", "http://whatsapp-bridge:3399")
    whatsapp_agent_name: str = _str("WHATSAPP_AGENT_NAME", "news-agent")
    whatsapp_token: str = _str("WHATSAPP_TOKEN", "")  # secret — env only, never the DSL

    # --- Bus identity ---
    sender_id: str = _str("SENDER_ID", "agent-runtime")

    # --- Startup resilience (services come up across separate compose projects) ---
    connect_retries: int = _int("CONNECT_RETRIES", 30)
    connect_retry_delay_s: int = _int("CONNECT_RETRY_DELAY_S", 2)

    # --- HTTP API (health + observability; localhost-bound, nginx fronts) ---
    api_host: str = _str("API_HOST", "0.0.0.0")
    api_port: int = _int("API_PORT", 6817)

    # --- Logging ---
    log_level: str = _str("LOG_LEVEL", "INFO")

    def farm_stream_key(self) -> str:
        """The full key of the farm's ingress stream: ``stream:<farm_stream_id>``."""
        return f"{self.stream_prefix}{self.farm_stream_id}"


settings = Settings()
