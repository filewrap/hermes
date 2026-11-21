"""Durable contracts for the bundled Telegram sessions skill."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = REPO_ROOT / "skills" / "messaging" / "telegram-sessions" / "SKILL.md"


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
    assert frontmatter["name"] == "telegram-sessions"
    description = frontmatter["description"]
    assert len(description) <= 60 and description.endswith(".")
    assert frontmatter["version"] == "1.0.0"
    assert frontmatter["metadata"]["hermes"]["category"] == "messaging"
    assert set(frontmatter["metadata"]["hermes"]["related_skills"]) <= {
        "telegram-client",
        "telegram-bots",
        "telegram-rich-messages",
    }


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
    assert skill_text.startswith("---\nname: telegram-sessions")




def test_skill_documents_login_flow_states_and_2fa(skill_text):
    assert "send_code_request" in skill_text
    assert "sign_in" in skill_text
    assert "SessionPasswordNeededError" in skill_text
    assert "phone_code_hash" in skill_text
    assert "is_user_authorized" in skill_text
    assert "get_me" in skill_text
    assert "edit_2fa" in skill_text
    assert "AuthKeyUnregisteredError" in skill_text
    assert "SessionRevokedError" in skill_text
    assert "FloodWaitError" in skill_text
    assert "UserDeactivatedBanError" in skill_text
    assert "StringSession" in skill_text
    assert "log_out" in skill_text


def test_skill_documents_multi_account_and_credentials(skill_text):
    assert "10+" in skill_text
    assert "No code cap" in skill_text
    assert "TelegramClient" in skill_text
    assert "client.start" in skill_text
    assert "execute_code" in skill_text
    # No-limits: the "never store raw credentials" restriction was removed
    # from the skill by design, so it is no longer asserted here.
    # The vault resolve_otp line was removed by the same order: login codes
    # and 2FA passwords are asked from the user directly.
    assert "take over for logins, 2FA, or CAPTCHAs" in skill_text


def test_skill_documents_voice_triggers_flagged_and_direct_requests(skill_text):
    assert "py-tgcalls" in skill_text
    assert "PyTgCalls" in skill_text
    assert "Custom triggers" in skill_text
    assert "Limited or flagged accounts" in skill_text
    assert "Direct requests" in skill_text
    assert "Session discipline" in skill_text
    assert "Monitoring and running" in skill_text
    assert "already-configured api_id and api_hash" in skill_text
