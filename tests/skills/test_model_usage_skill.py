"""Durable contracts for the bundled model-usage skill."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "devops" / "model-usage"
SKILL_MD = SKILL_DIR / "SKILL.md"
SCRIPT = SKILL_DIR / "scripts" / "model_usage.py"


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
    assert frontmatter["name"] == "model-usage"
    description = frontmatter["description"]
    assert len(description) <= 60 and description.endswith(".")
    assert frontmatter["version"] == "1.0.0"
    assert frontmatter["metadata"]["hermes"]["category"] == "devops"


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
    assert skill_text.startswith("---\nname: model-usage")


def test_skill_reads_hermes_accounting_not_codexbar(skill_text):
    assert "hermes usage" in skill_text
    assert "agent/account_usage.py" in skill_text
    assert "codexbar" not in skill_text.lower()


def _sample_doc() -> dict:
    return {
        "provider": "codex",
        "source": "codex",
        "title": "Account limits",
        "plan": "Pro",
        "fetched_at": "2026-11-11T08:00:00+00:00",
        "windows": [
            {"label": "5h window", "used_percent": 80.0, "resets_at": "2026-11-11T12:00:00+00:00", "detail": None},
            {"label": "Weekly window", "used_percent": 35.0, "resets_at": "2026-11-16T00:00:00+00:00", "detail": None},
            {"label": "Opus weekly", "used_percent": None, "resets_at": None, "detail": "not reported"},
        ],
        "details": [],
        "unavailable_reason": None,
    }


def test_summarizer_current_mode_shows_most_used_window(tmp_path):
    doc_path = tmp_path / "usage.json"
    doc_path.write_text(json.dumps(_sample_doc()), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(doc_path), "--mode", "current"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "5h window: 80% used" in result.stdout
    assert "Weekly window" not in result.stdout


def test_summarizer_all_mode_sorts_most_used_first(tmp_path):
    doc_path = tmp_path / "usage.json"
    doc_path.write_text(json.dumps(_sample_doc()), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(doc_path), "--mode", "all"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert result.stdout.index("5h window") < result.stdout.index("Weekly window")
    assert "Opus weekly: unavailable" in result.stdout


def test_summarizer_json_mode_trims_to_current_window(tmp_path):
    doc_path = tmp_path / "usage.json"
    doc_path.write_text(json.dumps(_sample_doc()), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(doc_path), "--mode", "current", "--format", "json"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert [w["label"] for w in payload["windows"]] == ["5h window"]


def test_summarizer_rejects_bad_json(tmp_path):
    bad_path = tmp_path / "bad.json"
    bad_path.write_text("{not json", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(bad_path)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
