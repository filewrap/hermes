"""Durable contracts for the bundled deep-research skill."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = REPO_ROOT / "skills" / "research" / "deep-research" / "SKILL.md"


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
    assert frontmatter["name"] == "deep-research"
    description = frontmatter["description"]
    assert len(description) <= 60 and description.endswith(".")
    assert frontmatter["version"] == "1.0.0"
    assert frontmatter["metadata"]["hermes"]["category"] == "research"
    assert "grounded-citations" in frontmatter["metadata"]["hermes"]["related_skills"]


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
    assert skill_text.startswith("---\nname: deep-research")


def test_skill_does_not_advertise_browser_reading(skill_text):
    """Browser sessions are not measured as read evidence, so the skill must not
    present browser tools as a route for finding and reading sources."""
    assert "browser tools" not in skill_text
    assert "browser reading" not in skill_text


def test_skill_documents_rounds_ledger_citations_and_limits(skill_text):
    assert "research_notes.md" in skill_text
    assert "report.md" in skill_text
    assert "`todo_list`" in skill_text
    assert "`web_search`" in skill_text
    assert "`web_extract`" in skill_text
    assert "200KB" in skill_text
    assert "References" in skill_text
    assert "Approval prompts are disabled" in skill_text
    assert "no purchases, public posts" in skill_text
    assert "`telegram_client`" in skill_text
    assert "`rich_message_send`" in skill_text
    assert "checkpoint.json" in skill_text
