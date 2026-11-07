"""Durable contracts for the bundled Telegram rich messages skill."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = REPO_ROOT / "skills" / "messaging" / "telegram-rich-messages" / "SKILL.md"


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
    assert frontmatter["name"] == "telegram-rich-messages"
    description = frontmatter["description"]
    assert len(description) <= 60 and description.endswith(".")
    assert frontmatter["version"] == "1.0.0"
    assert frontmatter["metadata"]["hermes"]["category"] == "messaging"
    assert "telegram-client" in frontmatter["metadata"]["hermes"]["related_skills"]


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
    assert skill_text.startswith("---\nname: telegram-rich-messages")


def test_skill_documents_tg_button_forms_limits_and_tool_call(skill_text):
    assert '<tg-button type="url"' in skill_text
    assert '<tg-button type="copy_text"' in skill_text
    assert '<tg-button type="callback_data"' in skill_text
    assert "<tg-button-row" in skill_text
    assert "256 characters" in skill_text
    assert "64 bytes" in skill_text
    assert "32,768" in skill_text
    assert "<p><i>Page 1 of 1" in skill_text
    assert "`telegram_client`" in skill_text
    assert '"rich_message_send"' in skill_text
    assert "WebView" in skill_text and "ButtonTypeInvalidError" in skill_text
    assert "PageBlockTitle" in skill_text
    assert "RICH_MESSAGE_BLOCK_UNSUPPORTED" in skill_text
