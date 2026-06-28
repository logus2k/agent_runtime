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
    """Holds the loaded records, keyed by id."""

    def __init__(self, agents_dir: str | Path):
        self._dir = Path(agents_dir)
        self._records: dict[str, AgentRecord] = {}

    def load_all(self) -> dict[str, AgentRecord]:
        """Load every ``*.yaml`` under the agents dir. Raises on the first bad
        file or duplicate id — partial/ambiguous registries are not allowed."""
        if not self._dir.is_dir():
            raise RecordLoadError(f"agents dir does not exist: {self._dir}")
        records: dict[str, AgentRecord] = {}
        files = sorted(self._dir.glob("*.yaml"))
        for path in files:
            rec = load_record(path)
            if rec.id in records:
                raise RecordLoadError(
                    f"{path}: duplicate agent id '{rec.id}' (already defined in "
                    f"another record)"
                )
            records[rec.id] = rec
            log.info("loaded agent record: %s (%s)", rec.id, path.name)
        self._records = records
        log.info("registry loaded %d agent(s) from %s", len(records), self._dir)
        return records

    def get(self, agent_id: str) -> AgentRecord | None:
        return self._records.get(agent_id)

    def require(self, agent_id: str) -> AgentRecord:
        """Resolve by id or raise — used on the dispatch path (the event named an
        agent we must run)."""
        rec = self._records.get(agent_id)
        if rec is None:
            raise KeyError(
                f"no agent record with id '{agent_id}' "
                f"(known: {sorted(self._records)})"
            )
        return rec

    def upsert(self, record: AgentRecord) -> None:
        """Add or replace a record in the live registry. Used by the admin deploy
        endpoint so a freshly-pushed agent is runnable on its next trigger without
        a process restart. The Farm shares this Registry instance, so the change is
        seen immediately by the next dispatch."""
        self._records[record.id] = record
        log.info("registry upsert: %s (now %d agent(s))", record.id, len(self._records))

    @property
    def ids(self) -> list[str]:
        return sorted(self._records)
