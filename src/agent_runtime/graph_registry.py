"""Graph-record registry — the live store of deployed workflow records (§9.3).

Keyed by the Project's immutable ``uid``. Deploy is **idempotent**: re-deploying the
same uid **updates the record in place and bumps its version's minor** (never
duplicates), matching "each Deploy increments the record's version" (§9.3). The farm
shares this instance, so a freshly-deployed workflow is runnable on its next trigger
without a restart.

**Persistence (§9.3):** each record is written as ``<graphs_dir>/<uid>.json`` on upsert
and removed on delete, and the whole dir is loaded on construction. A Deploy therefore
**survives a restart** — without this the in-memory registry lost every deployed Project
on reboot while the scheduler firing-binding lived on, leaving an orphaned ``record_uid``.
When ``store_dir`` is ``None`` the registry is purely in-memory (used by unit tests).

Loud + strict: an unknown uid raises on ``require``; a version that isn't ``major.minor``
is rejected by the model. No silent last-one-wins on a real conflict — upsert is the
explicit, intended in-place update. A corrupt record file on load raises loudly (never
silently skipped) — a deployed workflow that can't be read is a real, visible failure.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .dsl_graph import GraphRecord, _VERSION_RE

log = logging.getLogger("agent_runtime.graph_registry")


def _bump_minor(version: str) -> str:
    """``major.minor`` -> ``major.(minor+1)``. The major is preserved (a runtime that
    only speaks major N never rolls to N+1 on its own)."""
    m = _VERSION_RE.match(version)
    if not m:
        raise ValueError(f"cannot bump non 'major.minor' version '{version}'")
    return f"{m.group(1)}.{int(m.group(2)) + 1}"


class GraphRegistry:
    def __init__(self, store_dir: str | Path | None = None) -> None:
        self._records: dict[str, GraphRecord] = {}  # uid -> record
        self._dir: Path | None = Path(store_dir) if store_dir is not None else None
        if self._dir is not None:
            self._load_all()

    # ---- persistence ------------------------------------------------------
    def _path(self, uid: str) -> Path:
        assert self._dir is not None
        return self._dir / f"{uid}.json"

    def _load_all(self) -> None:
        """Load every ``*.json`` under the store dir into memory. Raises loudly on the
        first unreadable/corrupt record — a deployed workflow that can't be read must
        never be silently dropped."""
        assert self._dir is not None
        self._dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(self._dir.glob("*.json")):
            try:
                rec = GraphRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"cannot load graph record {path.name}: {exc}") from exc
            self._records[rec.uid] = rec
            log.info("loaded graph record: %s '%s' v%s (%s)",
                     rec.uid, rec.name, rec.version, path.name)
        log.info("graph registry loaded %d record(s) from %s",
                 len(self._records), self._dir)

    def _persist(self, record: GraphRecord) -> None:
        if self._dir is None:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path(record.uid).write_text(
            record.model_dump_json(indent=2), encoding="utf-8"
        )

    def _remove_file(self, uid: str) -> None:
        if self._dir is None:
            return
        self._path(uid).unlink(missing_ok=True)

    # ---- store ------------------------------------------------------------
    def get(self, uid: str) -> GraphRecord | None:
        return self._records.get(uid)

    def require(self, uid: str) -> GraphRecord:
        rec = self._records.get(uid)
        if rec is None:
            raise KeyError(
                f"no graph record with uid '{uid}' (known: {sorted(self._records)})"
            )
        return rec

    def upsert(self, record: GraphRecord, *, bump: bool = True) -> GraphRecord:
        """Add or replace a graph record, keyed by uid. If a record with this uid
        already exists and ``bump`` is set, the stored version's minor is incremented
        (idempotent re-deploy §9.3) — the NEW record is stored with the bumped version.
        On first insert the record keeps its own version. Returns the stored record."""
        existing = self._records.get(record.uid)
        if existing is not None and bump:
            new_version = _bump_minor(existing.version)
            record = record.model_copy(update={"version": new_version})
            log.info(
                "graph upsert (re-deploy): %s '%s' version %s -> %s",
                record.uid, record.name, existing.version, new_version,
            )
        else:
            log.info(
                "graph upsert (new): %s '%s' version %s",
                record.uid, record.name, record.version,
            )
        self._records[record.uid] = record
        self._persist(record)  # survive a restart (§9.3)
        return record

    def delete(self, uid: str) -> bool:
        existed = self._records.pop(uid, None) is not None
        if existed:
            self._remove_file(uid)
            log.info("graph delete: %s (now %d record(s))", uid, len(self._records))
        return existed

    def all(self) -> list[GraphRecord]:
        return sorted(self._records.values(), key=lambda r: r.name.lower())

    @property
    def uids(self) -> list[str]:
        return sorted(self._records)
