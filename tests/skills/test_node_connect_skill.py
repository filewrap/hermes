"""Durable contracts for the bundled node-connect skill.

The skill is the procedure; the script next to it is the deterministic
presentation. These tests prove the whole chain the skill prescribes
without a Telegram send: a diagnose result for every verdict builds a
mermaid flow the widget kit parses, a widget within the size limits, and
a rich message carrying the same steps — plus the script's CLI reads the
same JSON a saved diagnose result would hold.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "messaging" / "node-connect"
SKILL_MD = SKILL_DIR / "SKILL.md"
SCRIPT = SKILL_DIR / "scripts" / "node_diagnosis_flow.py"


def _parse_frontmatter(content: str) -> dict:
    from agent.skill_utils import parse_frontmatter

    frontmatter, _ = parse_frontmatter(content)
    return frontmatter


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "node_diagnosis_flow", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _diagnosis(verdict: str, states: dict[str, str]) -> dict:
    details = {
        "route": "The probe reached the node.",
        "auth": "The node accepted the token.",
        "pairing": "The node is paired.",
        "reconnect": "Answered the probe just now.",
    }
    steps = [
        {"name": name, "state": states[name], "detail": details[name]}
        for name in ("route", "auth", "pairing", "reconnect")
    ]
    diagnosis = {
        "ok": True,
        "action": "diagnose",
        "node": {"nodeId": "node_abc", "name": "phone-1", "platform": "android"},
        "verdict": verdict,
        "requestId": None,
        "steps": steps,
        "probe": {"answered": True, "http_status": 200, "error": None,
                  "payload": {"ok": True}},
        "next_step": "No fix needed — the node answers.",
    }
    if verdict == "pairing_pending":
        diagnosis["node"] = "phone-1"
        diagnosis["requestId"] = "pairreq_abc"
        diagnosis["probe"] = None
        diagnosis["next_step"] = "Approve the exact pairing request pairreq_abc."
    if verdict == "auth_failed":
        diagnosis["probe"] = {"answered": True, "http_status": 401,
                              "error": "bad token", "payload": None}
        diagnosis["next_step"] = "Re-pair with a fresh token."
    return diagnosis


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontmatter(skill_text: str) -> dict:
    return _parse_frontmatter(skill_text)


@pytest.fixture(scope="module")
def script():
    return _load_script()


def test_skill_file_and_frontmatter_are_complete(skill_text, frontmatter):
    assert SKILL_MD.is_file()
    assert skill_text.startswith("---")
    assert frontmatter["name"] == "node-connect"
    description = frontmatter["description"]
    assert len(description) <= 60 and description.endswith(".")
    assert frontmatter["version"] == "1.0.0"
    assert frontmatter["metadata"]["hermes"]["category"] == "messaging"
    assert "telegram-visualize" in frontmatter["metadata"]["hermes"]["related_skills"]
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
    assert skill_text.startswith("---\nname: node-connect")


def test_skill_names_the_diagnose_tool_and_the_flow_presentation(skill_text):
    assert "`nodes` diagnose" in skill_text
    assert '"widget_show"' in skill_text
    assert "flowchart LR" in skill_text
    assert "route" in skill_text and "reconnect" in skill_text
    for verdict in ("healthy", "unreachable", "auth_failed", "node_error",
                    "pairing_pending", "not_paired"):
        assert f"`{verdict}`" in skill_text


def test_skill_states_the_pairing_rules(skill_text):
    assert "Never approve by position" in skill_text
    assert "exact request ID" in skill_text
    assert "never write it anywhere else" in skill_text
    assert "approval, a paired count, or a ping proves only one transition" in skill_text


@pytest.mark.parametrize(
    "verdict,states",
    [
        ("healthy", {"route": "ok", "auth": "ok", "pairing": "ok", "reconnect": "ok"}),
        ("unreachable", {"route": "failed", "auth": "unknown", "pairing": "ok",
                         "reconnect": "failed"}),
        ("auth_failed", {"route": "ok", "auth": "failed", "pairing": "ok",
                         "reconnect": "skipped"}),
        ("node_error", {"route": "ok", "auth": "ok", "pairing": "ok",
                        "reconnect": "failed"}),
        ("pairing_pending", {"route": "unknown", "auth": "unknown",
                             "pairing": "pending", "reconnect": "skipped"}),
        ("not_paired", {"route": "unknown", "auth": "unknown",
                        "pairing": "failed", "reconnect": "skipped"}),
    ],
)
def test_script_builds_a_renderable_flow_for_every_verdict(script, verdict, states):
    """Every verdict the tool can return renders as a widget + rich message."""
    from plugins.platforms.telegram.widget_kit import (
        build_rich_html,
        build_widget_html,
        parse_mermaid_flowchart,
        validate_template_data,
    )

    presentation = script.build_presentation(_diagnosis(verdict, states))
    graph = parse_mermaid_flowchart(presentation["mermaid"])
    assert [graph.nodes[n].label for n in graph.order] == [
        f"{label}: {states[name]}"
        for name, label in (
            ("route", "Route"),
            ("auth", "Auth"),
            ("pairing", "Pairing"),
            ("reconnect", "Reconnect"),
        )
    ]
    params = presentation["widget_show_params"]
    assert params["template"] == "flow"
    data = validate_template_data(params["template"], params["data"])
    # The exact builders widget_show sends through: oversize is refused here.
    assert build_widget_html(params["template"], params["title"], data)
    rich = build_rich_html(params["template"], params["title"], data)
    assert "Node connection: phone-1" in rich
    assert verdict.replace("_", " ") in params["companion"].lower()
    for button in params["buttons"]:
        assert button["type"] == "copy"
        assert len(button["value"]) <= 256
    if verdict == "pairing_pending":
        assert any(b["label"] == "Copy request ID" for b in params["buttons"])
    else:
        assert not any(b["label"] == "Copy request ID" for b in params["buttons"])


def test_script_rejects_a_non_diagnose_result(script):
    with pytest.raises(SystemExit):
        script.build_presentation({"ok": False, "error": "node is required."})
    with pytest.raises(SystemExit):
        script.build_presentation({"ok": True, "verdict": "mystery", "steps": []})


def test_script_cli_reads_a_saved_diagnose_json(tmp_path, script):
    diagnosis = _diagnosis(
        "auth_failed",
        {"route": "ok", "auth": "failed", "pairing": "ok", "reconnect": "skipped"},
    )
    path = tmp_path / "diagnosis.json"
    path.write_text(json.dumps(diagnosis), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    presentation = json.loads(proc.stdout)
    assert presentation["mermaid"].startswith("flowchart LR")
    assert presentation["widget_show_params"]["template"] == "flow"
    assert "Auth: failed" in presentation["mermaid"]
