"""Agent registry — loads runtime DSL records from ``data/agents/*.yaml``.

The farm resolves a trigger event's ``{ agent: <id> }`` to a record here. Loading
is strict and loud: a malformed record raises with the offending file named, and a
duplicate id is a hard error (never a silent last-one-wins).
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from .dsl import AgentRecord

log = logging.getLogger("agent_runtime.registry")


class RecordLoadError(Exception):
    """A record file could not be parsed or validated. Carries the file path."""


def load_record(path: Path) -> AgentRecord:
    """Parse + validate one record file. Raises RecordLoadError with context."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RecordLoadError(f"{path}: cannot read/parse YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise RecordLoadError(
            f"{path}: top-level YAML must be a mapping, got {type(raw).__name__}"
        )
    try:
        return AgentRecord.model_validate(raw)
    except ValidationError as exc:
        raise RecordLoadError(f"{path}: invalid agent record:\n{exc}") from exc


class Registry:
    """Holds the loaded records, keyed by the immutable ``uid``.

    A secondary by-name index supports display lookups and the legacy routing
    fallback (an event that carries the friendly name in ``event_data.agent``
    instead of ``agent_uid``). Names are not identity, so a name collision is a
    loud warning, not a hard error — the uid always disambiguates."""

    def __init__(self, agents_dir: str | Path):
        self._dir = Path(agents_dir)
        self._records: dict[str, AgentRecord] = {}        # uid -> record
        self._by_name: dict[str, AgentRecord] = {}        # name -> record (best effort)

    def _reindex(self) -> None:
        by_name: dict[str, AgentRecord] = {}
        for rec in self._records.values():
            if rec.name in by_name:
                log.warning(
                    "two agent records share the name '%s' (uids %s, %s) — name lookups "
                    "are ambiguous; routing by uid is unaffected",
                    rec.name, by_name[rec.name].uid, rec.uid,
                )
            by_name[rec.name] = rec
        self._by_name = by_name

    def load_all(self) -> dict[str, AgentRecord]:
        """Load every ``*.yaml`` under the agents dir. Raises on the first bad
        file or duplicate uid — partial/ambiguous registries are not allowed."""
        if not self._dir.is_dir():
            raise RecordLoadError(f"agents dir does not exist: {self._dir}")
        records: dict[str, AgentRecord] = {}
        files = sorted(self._dir.glob("*.yaml"))
        for path in files:
            rec = load_record(path)
            if rec.uid in records:
                raise RecordLoadError(
                    f"{path}: duplicate agent uid '{rec.uid}' (already defined in "
                    f"another record)"
                )
            records[rec.uid] = rec
            log.info("loaded agent record: %s '%s' (%s)", rec.uid, rec.name, path.name)
        self._records = records
        self._reindex()
        log.info("registry loaded %d agent(s) from %s", len(records), self._dir)
        return records

    def get(self, uid: str) -> AgentRecord | None:
        return self._records.get(uid)

    def get_by_name(self, name: str) -> AgentRecord | None:
        """Resolve by friendly name (legacy routing fallback + display)."""
        return self._by_name.get(name)

    def require(self, uid: str) -> AgentRecord:
        """Resolve by uid or raise — used on the dispatch path."""
        rec = self._records.get(uid)
        if rec is None:
            raise KeyError(
                f"no agent record with uid '{uid}' (known: {sorted(self._records)})"
            )
        return rec

    def upsert(self, record: AgentRecord) -> None:
        """Add or replace a record in the live registry. Used by the admin
        create/deploy endpoints so a freshly-pushed agent is runnable on its next
        trigger without a process restart. The Farm shares this Registry instance,
        so the change is seen immediately by the next dispatch."""
        self._records[record.uid] = record
        self._reindex()
        log.info(
            "registry upsert: %s '%s' (now %d agent(s))",
            record.uid, record.name, len(self._records),
        )

    def delete(self, uid: str) -> bool:
        """Remove a record from the live registry. Returns True if it existed."""
        existed = self._records.pop(uid, None) is not None
        if existed:
            self._reindex()
            log.info("registry delete: %s (now %d agent(s))", uid, len(self._records))
        return existed

    def all(self) -> list[AgentRecord]:
        """Every record, sorted by name (for the admin list view)."""
        return sorted(self._records.values(), key=lambda r: r.name.lower())

    @property
    def ids(self) -> list[str]:
        """All uids (sorted). Named ``ids`` for back-compat with existing callers."""
        return sorted(self._records)
