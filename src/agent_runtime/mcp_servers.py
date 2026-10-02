"""MCP server registry — the writable list of MCP hosts.

Two layers, deliberately:

* **Env-declared hosts** (``MCP_SERVER_KEY``/``MCP_URL`` + ``MCP_SERVERS``) are the
  immutable deployment defaults — they describe the hosts this deployment ships with.
* **Runtime-registered hosts** live here as one JSON per key
  (``data/mcp_servers/<key>.json`` -> ``{key, url}``) — the same file-per-entity idiom
  as ``data/agents`` / ``data/graphs``, so a host added from the Resource Manager
  survives a restart and is inspectable/diffable on disk.

Merge order is env first, then the file store (a stored entry with the same key wins,
so an operator can repoint a host without a redeploy). Reads hit the directory each
call — a host added through the API is visible immediately, no restart.

Writes are atomic (temp file + ``os.replace``) so a crash mid-write can never leave a
half-written host that would break the whole listing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from typing import Any

from .config import settings

log = logging.getLogger("agent_runtime.mcp_servers")

# A key becomes a tool-name prefix (``<key>__<tool>``) and a filename, so constrain it.
# "__" is excluded because it is the namespace separator itself.
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ServerError(ValueError):
    """A rejected server definition (bad key or URL)."""


def _dir() -> str:
    return settings.mcp_servers_dir


def validate(key: str, url: str) -> tuple[str, str]:
    """Normalize + reject a bad definition loudly (never silently drop a host)."""
    key = (key or "").strip()
    url = (url or "").strip()
    if not KEY_RE.match(key):
        raise ServerError(
            f"invalid server key {key!r}: use letters/digits/._- and start alphanumeric"
        )
    if "__" in key:
        raise ServerError(
            f"invalid server key {key!r}: '__' is the tool namespace separator"
        )
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ServerError(f"invalid url {url!r}: must start with http:// or https://")
    return key, url


def stored() -> dict[str, str]:
    """Runtime-registered hosts only (``{key: url}``). Missing dir = none.

    A single unreadable/corrupt file is logged and skipped rather than failing the
    whole listing — one bad host must not hide the good ones."""
    out: dict[str, str] = {}
    d = _dir()
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(d, name)
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
            key = str(rec.get("key") or os.path.splitext(name)[0]).strip()
            url = str(rec.get("url") or "").strip()
            if key and url:
                out[key] = url
            else:
                log.warning("mcp server file %s has no key/url; skipped", path)
        except Exception as exc:  # noqa: BLE001 - one bad file must not break listing
            log.warning("unreadable mcp server file %s: %s", path, exc)
    return out


def all_servers() -> dict[str, str]:
    """Every reachable host: env defaults, then the file store (store wins on a clash)."""
    merged = dict(settings.mcp_servers())   # env-declared
    merged.update(stored())                 # runtime-registered
    return merged


def url_for(key: str) -> str | None:
    return all_servers().get(key)


def items() -> list[dict[str, Any]]:
    """List shape for the Resource picker/manager. ``source`` tells the UI which hosts
    are deployment defaults (not deletable) vs operator-added."""
    env_keys = set(settings.mcp_servers())
    st = stored()
    out: list[dict[str, Any]] = []
    for key, url in sorted(all_servers().items()):
        out.append({
            "key": key,
            "url": url,
            "source": "runtime" if key in st else "env",
        })
    return out


def put(key: str, url: str) -> dict[str, Any]:
    """Create or update a host (idempotent upsert). Returns the stored record."""
    key, url = validate(key, url)
    d = _dir()
    os.makedirs(d, exist_ok=True)
    rec = {"key": key, "url": url}
    path = os.path.join(d, f"{key}.json")
    # Atomic: write a temp file in the same dir, then replace — a reader never sees a
    # partially-written host.
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{key}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=2)
            fh.write("\n")
        # mkstemp creates 0600; these files are meant to be readable/diffable on the host
        # (that inspectability is why they are files at all), so match the 0644 the other
        # record stores use.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    log.info("mcp server registered: %s -> %s", key, url)
    return rec


def delete(key: str) -> bool:
    """Remove a runtime-registered host. Env-declared hosts cannot be deleted (they are
    deployment config, not data) — that is reported loudly rather than silently ignored."""
    key = (key or "").strip()
    if key not in stored():
        if key in settings.mcp_servers():
            raise ServerError(
                f"'{key}' is declared in the environment (MCP_SERVERS/MCP_URL) and "
                "cannot be deleted here — change the deployment config instead"
            )
        return False
    path = os.path.join(_dir(), f"{key}.json")
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    log.info("mcp server removed: %s", key)
    return True
