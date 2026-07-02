"""Skills subsystem — reusable, focused knowledge units injected into an agent's
context (block_management.md §8.3).

Agent Runtime **owns and serves** its skills from its OWN dedicated folder structure
(``data/skills/<name>/SKILL.md`` and/or ``data/domains/<id>/skills/<name>/SKILL.md``) —
it does NOT read noted's data. The ``SKILL.md`` format/parser is reused from noted's
proven model (``noted/backend/app/managers/llm_skills.py``), but the store is ours.

Public surface:
  * ``Skill`` / ``SkillRegistry`` — the parser + registry (this package's ``registry``).
  * ``get_registry()`` — a process-level singleton bound to ``settings.skills_dir``.
"""

from __future__ import annotations

from .registry import Skill, SkillRegistry, get_registry

__all__ = ["Skill", "SkillRegistry", "get_registry"]
