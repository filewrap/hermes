"""Durable contracts for the bundled Telegram visualize skill."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = REPO_ROOT / "skills" / "messaging" / "telegram-visualize" / "SKILL.md"


def _parse_frontmatter(content: str) -> dict:
    from agent.skill_utils import parse_frontmatter

    frontmatter, _ = parse_frontmatter(content)
    return frontmatter


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontmatter(skill_text: str) -> dict:
    return _parse_frontmatter(skill_text)


def test_skill_file_and_frontmatter_are_complete(skill_text, frontmatter):
    assert SKILL_MD.is_file()
    assert skill_text.startswith("---")
    assert frontmatter["name"] == "telegram-visualize"
    description = frontmatter["description"]
    assert len(description) <= 60 and description.endswith(".")
    assert frontmatter["version"] == "1.0.0"
    assert frontmatter["metadata"]["hermes"]["category"] == "messaging"
    assert "telegram-widgets" in frontmatter["metadata"]["hermes"]["related_skills"]
    assert "telegram-rich-messages" in frontmatter["metadata"]["hermes"]["related_skills"]


def test_skill_sections_follow_the_modern_order(skill_text):
    headings = re.findall(r"^## (.+)$", skill_text, re.MULTILINE)
    assert headings == [
        "When to Use",
        "Prerequisites",
        "How to Run",
        "Quick Reference",
        "Procedure",
        "Pitfalls",
        "Verification",
    ]
    assert skill_text.startswith("---\nname: telegram-visualize")


def test_skill_documents_widget_show_templates_and_routing(skill_text):
    assert '"widget_show"' in skill_text
    for template in ("metric", "progress", "checklist", "flow", "chart", "status"):
        assert f"`{template}`" in skill_text
    assert "flowchart TD" in skill_text
    assert "flowchart LR" in skill_text
    assert "<canvas>" in skill_text
    assert "surface" in skill_text
    assert "DM" in skill_text


def test_skill_states_dm_only_widgets_and_no_dashboard_pinning(skill_text):
    assert "Widgets go to DMs only" in skill_text
    assert "rich message instead" in skill_text
    assert "does not pin widgets to a dashboard" in skill_text
    assert "no tap handler" in skill_text
