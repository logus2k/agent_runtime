"""Skill registry tests (block_management.md §8.3): SKILL.md parsing (triggers /
priority / max_tokens), the two-layout scan (flat + per-domain), get_static_skills
(priority-1 on matching context + budget), and get_skill (returns / errs)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime.skills.registry import (
    STATIC_SKILL_TOKEN_BUDGET,
    Skill,
    SkillRegistry,
)

FIXTURES = Path(__file__).parent / "skill_fixtures"


@pytest.fixture
def registry() -> SkillRegistry:
    return SkillRegistry(str(FIXTURES))


def test_parses_frontmatter_fields(registry: SkillRegistry):
    s = registry.get_skill_info("news-curation")
    assert s is not None
    assert s.name == "news-curation"
    assert s.description.startswith("How to curate")
    assert s.triggers == ["news", "morning_digest"]
    assert s.priority == 1
    assert s.max_tokens == 400
    # Body is the markdown after the frontmatter.
    assert "Group related headlines" in s.content


def test_defaults_priority_and_max_tokens():
    """A SKILL.md without priority/max_tokens gets the documented defaults (3, 500)."""
    parsed = SkillRegistry.__new__(SkillRegistry)  # bypass __init__/scan
    skill = parsed._parse_skill.__func__(  # type: ignore[attr-defined]
        parsed,
        _write_tmp_skill(),
        str(FIXTURES),
        "minimal",
        "default",
    )
    assert skill.priority == 3
    assert skill.max_tokens == 500
    assert skill.triggers == []


def _write_tmp_skill() -> str:
    import tempfile

    d = tempfile.mkdtemp()
    p = Path(d) / "SKILL.md"
    p.write_text("---\nname: minimal\ndescription: bare\n---\nbody here\n")
    return str(p)


def test_scans_both_flat_and_per_domain(registry: SkillRegistry):
    names = {name for name, _ in registry.list_skills()}
    # flat layout
    assert "news-curation" in names
    assert "deep-analysis" in names
    # per-domain layout: data/domains/<id>/skills/<name>/SKILL.md
    assert "market-rules" in names
    assert registry.get_skill_info("market-rules").domain_id == "finance"
    assert registry.get_skill_info("news-curation").domain_id == "default"


def test_get_static_skills_returns_priority1_on_matching_context(registry: SkillRegistry):
    matched = registry.get_static_skills({"news"})
    names = {n for n, _ in matched}
    # priority-1 + trigger 'news' matches
    assert "news-curation" in names
    # priority-3 skill never auto-injects, even if its trigger fires
    assert "deep-analysis" not in names
    # market-rules is priority-1 but its trigger ('markets') did not fire
    assert "market-rules" not in names


def test_get_static_skills_empty_when_no_trigger_matches(registry: SkillRegistry):
    assert registry.get_static_skills({"unrelated"}) == []


def test_get_static_skills_respects_allow_list(registry: SkillRegistry):
    # 'news' fires news-curation, but it's not in the allow-list -> excluded.
    assert registry.get_static_skills({"news"}, names=["market-rules"]) == []
    # both triggers fire; only the allowed one is returned.
    matched = registry.get_static_skills(
        {"news", "markets"}, names=["market-rules"]
    )
    assert {n for n, _ in matched} == {"market-rules"}


def test_get_static_skills_raises_over_budget(tmp_path):
    """A priority-1 skill whose body blows the token budget RAISES (never truncates)."""
    big = tmp_path / "huge"
    big.mkdir()
    words = "word " * (STATIC_SKILL_TOKEN_BUDGET)  # ~1.3x over budget in estimated tokens
    (big / "SKILL.md").write_text(
        f"---\nname: huge\ndescription: big\ntriggers: [boom]\npriority: 1\n---\n{words}"
    )
    reg = SkillRegistry(str(tmp_path))
    with pytest.raises(RuntimeError, match="token budget"):
        reg.get_static_skills({"boom"})


def test_get_skill_returns_body_and_none(registry: SkillRegistry):
    body = registry.get_skill("news-curation")
    assert body is not None and "Group related headlines" in body
    assert registry.get_skill("does-not-exist") is None


def test_get_registry_text_lists_allowed(registry: SkillRegistry):
    text = registry.get_registry_text(names=["news-curation"])
    assert "news-curation" in text
    assert "deep-analysis" not in text
    # unfiltered lists everything
    full = registry.get_registry_text()
    assert "deep-analysis" in full and "market-rules" in full


def test_bad_frontmatter_is_skipped_not_fatal(tmp_path, caplog):
    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / "SKILL.md").write_text("no frontmatter here, just text")
    good = tmp_path / "ok"
    good.mkdir()
    (good / "SKILL.md").write_text("---\nname: ok\ndescription: fine\n---\nbody")
    reg = SkillRegistry(str(tmp_path))
    names = {n for n, _ in reg.list_skills()}
    assert names == {"ok"}  # broken skipped, good loaded
