"""Shape of the <available_skills> catalog (<skill> blocks, locations, clipped descriptions)."""
import re

import pytest

from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache
from agent.skill_utils import SKILL_PROMPT_DESC_LIMIT, clip_skill_description
from agent.skills_index_delta import index_entries

LONG = "Use when cutting, publishing, or abandoning a hermes-agent release and its notes."


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_skills_system_prompt_cache(clear_snapshot=True)
    yield
    clear_skills_system_prompt_cache(clear_snapshot=True)


def _skill(root, rel, desc):
    d = root / rel
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {d.name}\ndescription: {desc}\n---\n\nBody.\n", encoding="utf-8")


def test_index_blocks_carry_each_name_once_with_location_and_clipped_description(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    skills = tmp_path / "skills"
    _skill(skills, "creative/ascii-art", "ASCII art banners.")
    _skill(skills, "creative/pixel-art", "Pixel art sprites.")
    (skills / "creative" / "DESCRIPTION.md").write_text(
        "---\ndescription: Creative content generation and visual design tools.\n---\n", encoding="utf-8")
    _skill(skills, "computer-use", "Drive the desktop.")  # a top-level skill is its own category
    _skill(skills, "github/hermes-release", LONG)

    prompt = build_skills_system_prompt()
    block = re.search(r"<available_skills>\n(.*?)\n</available_skills>", prompt, re.DOTALL).group(1)
    # Ported shape: one <skill> block per skill, name + verbatim description + location.
    assert block.count("<skill>") == 4 and block.count("</skill>") == 4
    assert "<name>ascii-art</name>" in block and "<name>pixel-art</name>" in block
    assert "<name>computer-use</name>" in block and "<name>hermes-release</name>" in block
    assert "<location>creative/ascii-art/SKILL.md</location>" in block
    assert "<location>computer-use/SKILL.md</location>" in block
    assert "<location>github/hermes-release/SKILL.md</location>" in block
    assert "<description>ASCII art banners.</description>" in block
    assert "<description>Drive the desktop.</description>" in block
    # No category headers: the catalog is flat, name-sorted.
    assert "creative:" not in block and "Creative content generation" not in block
    release = re.search(
        r"<name>hermes-release</name>\n    <description>([^<]*)</description>", block).group(1)
    assert len(release) <= SKILL_PROMPT_DESC_LIMIT and release.endswith("...")
    kept = release[:-3]
    assert LONG.startswith(kept) and LONG[len(kept)] in " ,"  # cut lands between words
    # The turn-delta parser reads the new blocks like the old rows.
    assert set(index_entries(prompt)) == {"ascii-art", "pixel-art", "computer-use", "hermes-release"}


def test_catalog_compacts_descriptions_only_under_a_small_budget(monkeypatch, tmp_path):
    """Ported rule: targetChars = floor(contextTokenBudget / 5); over budget,
    only descriptions shrink — names, locations and loading notes never drop."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    skills = tmp_path / "skills"
    _skill(skills, "creative/ascii-art", "ASCII art banners for terminal headers.")
    _skill(skills, "creative/pixel-art", "Pixel art sprites for retro games.")
    _skill(skills, "github/hermes-release", LONG)
    _skill(skills, "computer-use", "Drive the desktop with mouse and keyboard.")

    full = build_skills_system_prompt()
    floored = build_skills_system_prompt(context_token_budget=1)
    assert len(floored) < len(full)  # floor render is strictly shorter
    assert "<name>ascii-art</name>" in floored and "<location>creative/ascii-art/SKILL.md</location>" in floored
    mid_budget = ((len(full) + len(floored)) // 2) * 5
    compacted = build_skills_system_prompt(context_token_budget=mid_budget)
    assert len(compacted) <= mid_budget // 5
    for name in ("ascii-art", "pixel-art", "hermes-release", "computer-use"):
        assert f"<name>{name}</name>" in compacted
    assert "creative/ascii-art/SKILL.md" in compacted
    assert "Scan <available_skills> for a matching workflow." in compacted
    # At least one description shrank below its full rendering.
    assert len(compacted) < len(full)


@pytest.mark.parametrize("sep", ["\t", "\u00a0"], ids=["tab", "nbsp"])
def test_clip_cuts_at_any_whitespace_never_inside_a_word(sep):
    desc = f"alpha{sep}" + f"bravo{sep}" * 12
    shown = clip_skill_description(desc)
    assert len(shown) <= SKILL_PROMPT_DESC_LIMIT and shown.endswith("...")
    assert set(shown[:-3].split(sep)) == {"alpha", "bravo"}  # whole words only, no "bra..."
