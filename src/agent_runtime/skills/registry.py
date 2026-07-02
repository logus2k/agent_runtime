"""Skill registry — the parser + store, ported from noted's proven ``SKILL.md`` model.

A **skill = a folder with a ``SKILL.md``** (block_management.md §8.3): YAML frontmatter
(``name``, ``description``, ``triggers``, ``priority`` default 3, ``max_tokens`` default
500) + a markdown body (the instruction text injected into context). Optional
``references/`` / ``scripts/`` / ``assets/`` subfolders.

Agent Runtime owns its OWN store — this scans two layouts under one data root:
  * flat:       ``<root>/<skill>/SKILL.md``
  * per-domain: ``<root>/domains/<domain_id>/skills/<skill>/SKILL.md``

The registry provides:
  * ``get_registry_text(...)`` — the name+description advertisement for the system prompt.
  * ``get_static_skills(context_conditions, ...)`` — priority-1 skills whose triggers
    match the current context, within a hard token budget (RAISES if exceeded — never
    silently truncates).
  * ``get_skill(name)`` — the body for on-demand fetch (the ``get_skill`` tool).

Parsing failures are logged loudly and skip the offending skill (never swallow); a bad
``SKILL.md`` does not take down the registry.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Iterable, Optional

log = logging.getLogger("agent_runtime.skills")

# Hard cap for auto-injected priority-1 skills (mirrors noted's ~32k). get_static_skills
# RAISES if the matched set exceeds this — surfaced loudly, never silently truncated.
STATIC_SKILL_TOKEN_BUDGET = 32000

# Rough tokens-per-word heuristic (same as noted): word count * 1.3.
_TOKENS_PER_WORD = 1.3


class Skill:
    """A single skill loaded from a ``SKILL.md`` file."""

    __slots__ = (
        "name",
        "description",
        "triggers",
        "priority",
        "max_tokens",
        "content",
        "folder_path",
        "domain_id",
    )

    def __init__(
        self,
        name: str,
        description: str,
        triggers: list[str],
        priority: int,
        max_tokens: int,
        content: str,
        folder_path: str,
        domain_id: str,
    ) -> None:
        self.name = name
        self.description = description
        self.triggers = triggers
        self.priority = priority
        self.max_tokens = max_tokens
        self.content = content
        self.folder_path = folder_path
        self.domain_id = domain_id


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


class SkillRegistry:
    """Loads and manages Agent Runtime's own skill library."""

    def __init__(self, skills_dir: str) -> None:
        self._skills_dir = os.path.abspath(skills_dir)
        self._skills: dict[str, Skill] = {}  # name -> Skill
        self._load_all()

    # ── loading ─────────────────────────────────────────────────────────────

    def _load_all(self) -> None:
        """Scan both the flat layout and the per-domain layout under our data root."""
        root = self._skills_dir
        if not os.path.isdir(root):
            log.warning("skills dir not found: %s (registry is empty)", root)
            return

        # 1) flat: <root>/<skill>/SKILL.md — bucket under the platform domain "default".
        flat = self._scan_skills_root(root, "default")
        log.info("loaded %d skills from %s", flat, root)

        # 2) per-domain: <root>/domains/<domain_id>/skills/<skill>/SKILL.md
        domains_dir = os.path.join(root, "domains")
        if os.path.isdir(domains_dir):
            for domain_id in sorted(os.listdir(domains_dir)):
                domain_path = os.path.join(domains_dir, domain_id)
                skills_root = os.path.join(domain_path, "skills")
                if not os.path.isdir(skills_root):
                    continue
                count = self._scan_skills_root(skills_root, domain_id)
                log.info(
                    "loaded %d skills from %s/skills/", count, domain_path
                )

    def _scan_skills_root(self, skills_root: str, domain_id: str) -> int:
        """Walk one directory and register each ``<skill>/SKILL.md``. The per-domain
        ``domains/`` subfolder under the flat root is skipped (it's scanned separately)."""
        count = 0
        for entry in sorted(os.listdir(skills_root)):
            entry_path = os.path.join(skills_root, entry)
            if not os.path.isdir(entry_path):
                continue
            # Skip the domains/ pivot and dot/underscore folders in the flat scan.
            if entry in ("domains", "_archive") or entry.startswith((".", "_")):
                continue
            skill_md = os.path.join(entry_path, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue
            skill = self._parse_skill(skill_md, entry_path, entry, domain_id)
            if skill is None:
                continue
            if skill.name in self._skills:
                prev = self._skills[skill.name]
                log.warning(
                    "skill name collision: '%s' redefined by domain '%s' (was '%s'); "
                    "later registration wins — names should be unique across domains.",
                    skill.name, domain_id, prev.domain_id,
                )
            self._skills[skill.name] = skill
            count += 1
        return count

    def _parse_skill(
        self, skill_md_path: str, folder_path: str, folder_name: str, domain_id: str
    ) -> Optional[Skill]:
        """Parse one ``SKILL.md`` (YAML frontmatter + body). Bad frontmatter -> None
        (logged loudly), never a silent registration of garbage."""
        try:
            with open(skill_md_path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as exc:
            log.warning("could not read %s: %s", skill_md_path, exc)
            return None

        match = _FRONTMATTER_RE.match(text)
        if not match:
            log.warning("skill %s/SKILL.md has no valid --- frontmatter ---", folder_name)
            return None

        frontmatter_text = match.group(1)
        content = match.group(2).strip()
        fm = _parse_frontmatter(frontmatter_text)

        name = fm.get("name") or folder_name
        description = fm.get("description", "")
        triggers = fm.get("triggers", [])
        if isinstance(triggers, str):
            triggers = [triggers]
        priority = fm.get("priority", 3)
        max_tokens = fm.get("max_tokens", 500)
        # Coerce numeric fields defensively (a quoted "1" in YAML is a str).
        priority = _as_int(priority, default=3)
        max_tokens = _as_int(max_tokens, default=500)

        return Skill(
            name=str(name),
            description=str(description),
            triggers=[str(t) for t in triggers],
            priority=priority,
            max_tokens=max_tokens,
            content=content,
            folder_path=folder_path,
            domain_id=domain_id,
        )

    # ── query API (advertise / auto-inject / on-demand) ─────────────────────

    def get_skill(self, name: str) -> Optional[str]:
        """The ``SKILL.md`` body for ``name`` (the ``get_skill`` tool's fetch), or None."""
        skill = self._skills.get(name)
        return skill.content if skill else None

    def get_skill_info(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def list_skills(self) -> list[tuple[str, dict]]:
        """(name, metadata) for every loaded skill — the ``/resources/skill`` source."""
        return [
            (
                s.name,
                {
                    "name": s.name,
                    "description": s.description,
                    "triggers": s.triggers,
                    "priority": s.priority,
                    "max_tokens": s.max_tokens,
                    "domain_id": s.domain_id,
                },
            )
            for s in sorted(self._skills.values(), key=lambda s: s.name)
        ]

    def get_registry_text(self, names: Optional[Iterable[str]] = None) -> str:
        """The registry advertisement (name + description) for the system prompt.

        When ``names`` is given, only those skills are listed (the Agent block's
        selected skills allow-list). When None, every loaded skill is listed."""
        allow = set(names) if names is not None else None
        lines = ["Available skills (use the get_skill tool to load a skill's instructions):"]
        listed = 0
        for skill in sorted(self._skills.values(), key=lambda s: s.name):
            if allow is not None and skill.name not in allow:
                continue
            lines.append(f"- {skill.name}: {skill.description}")
            listed += 1
        if listed == 0:
            return ""
        return "\n".join(lines)

    def get_static_skills(
        self,
        context_conditions: Iterable[str],
        names: Optional[Iterable[str]] = None,
    ) -> list[tuple[str, str]]:
        """Priority-1 skills whose triggers match the context, within the token budget.

        Args:
            context_conditions: condition strings for the current run (e.g.
                ``{"news", "morning"}``). A skill fires if ANY of its triggers is in
                this set.
            names: optional allow-list (the Agent block's selected skills). When given,
                only those skills are considered; when None, all loaded skills are.

        Returns:
            ``[(name, body), ...]`` for matching priority-1 skills.

        Raises:
            RuntimeError if the matched bodies exceed ``STATIC_SKILL_TOKEN_BUDGET`` —
            surfaced loudly (never silently truncated).
        """
        conditions = set(context_conditions)
        allow = set(names) if names is not None else None
        matched: list[tuple[str, str]] = []
        total_tokens_est = 0.0

        for skill in sorted(self._skills.values(), key=lambda s: s.name):
            if skill.priority != 1:
                continue
            if not skill.triggers:
                continue
            if allow is not None and skill.name not in allow:
                continue
            if any(t in conditions for t in skill.triggers):
                est = len(skill.content.split()) * _TOKENS_PER_WORD
                matched.append((skill.name, skill.content))
                total_tokens_est += est

        if total_tokens_est > STATIC_SKILL_TOKEN_BUDGET:
            raise RuntimeError(
                f"auto-injected priority-1 skills exceed the "
                f"{STATIC_SKILL_TOKEN_BUDGET}-token budget (estimated "
                f"{int(total_tokens_est)} tokens across {len(matched)} skills: "
                f"{[name for name, _ in matched]}). Trim skill content or lower priority."
            )

        return matched


# --------------------------------------------------------------------------- #
# frontmatter helpers (minimal YAML — no PyYAML dependency for frontmatter,
# matching noted's parser so the same SKILL.md files parse identically).
# --------------------------------------------------------------------------- #
def _parse_frontmatter(frontmatter_text: str) -> dict:
    fm: dict = {}
    for line in frontmatter_text.split("\n"):
        line = line.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            value = [
                v.strip().strip('"').strip("'")
                for v in value[1:-1].split(",")
                if v.strip()
            ]
        elif value.isdigit():
            value = int(value)
        else:
            value = value.strip('"').strip("'")
        fm[key] = value
    return fm


def _as_int(value, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


# --------------------------------------------------------------------------- #
# Process-level singleton bound to settings.skills_dir.
# --------------------------------------------------------------------------- #
_registry: Optional[SkillRegistry] = None


def get_registry(skills_dir: Optional[str] = None) -> SkillRegistry:
    """Get or create the global skill registry. First call fixes the dir; pass
    ``skills_dir`` explicitly (tests) to bind a different store."""
    global _registry
    if _registry is None or skills_dir is not None:
        if skills_dir is None:
            from ..config import settings

            skills_dir = settings.skills_dir
        reg = SkillRegistry(skills_dir)
        if skills_dir is not None and _registry is not None:
            # An explicit dir (tests) builds a fresh registry but does NOT clobber the
            # process singleton, so test isolation can't leak into a running app.
            return reg
        _registry = reg
    return _registry
