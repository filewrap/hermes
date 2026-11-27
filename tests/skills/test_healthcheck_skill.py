"""Durable contracts for the bundled healthcheck skill."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = REPO_ROOT / "skills" / "devops" / "healthcheck" / "SKILL.md"


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
    assert frontmatter["name"] == "healthcheck"
    description = frontmatter["description"]
    assert len(description) <= 60 and description.endswith(".")
    assert frontmatter["version"] == "1.0.0"
    assert frontmatter["metadata"]["hermes"]["category"] == "devops"
    assert "Security" in frontmatter["metadata"]["hermes"]["tags"]


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
    assert skill_text.startswith("---\nname: healthcheck")


def test_ask_before_change_stays_skill_text_not_a_gate(skill_text):
    assert "Ask before state-changing actions." in skill_text
    assert "hermes doctor --fix" in skill_text
    # The skill never claims Hermes manages the OS surface it audits.
    assert "Never claim Hermes manages the OS firewall, SSH, or updates." in skill_text
