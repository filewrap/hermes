"""Durable contracts for the bundled Telegram widgets skill."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = REPO_ROOT / "skills" / "messaging" / "telegram-widgets" / "SKILL.md"


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
    assert frontmatter["name"] == "telegram-widgets"
    description = frontmatter["description"]
    assert len(description) <= 60 and description.endswith(".")
    assert frontmatter["version"] == "1.0.0"
    assert frontmatter["metadata"]["hermes"]["category"] == "messaging"
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
    assert skill_text.startswith("---\nname: telegram-widgets")


def test_skill_documents_widget_rules_and_action_map(skill_text):
    assert '"widget_send"' in skill_text
    assert "`widget_answer`" in skill_text
    assert "`widget_edit_shared`" in skill_text
    assert "`widget_get_public_data`" in skill_text
    assert "32,768" in skill_text
    assert "4,096" in skill_text
    assert "1,024" in skill_text
    assert "widget_measure.py" in skill_text
    assert "Never retry" in skill_text
    assert "beta client" in skill_text.lower()
    assert "edit state before answering" in skill_text.lower()
    assert "`telegram_client`" in skill_text
