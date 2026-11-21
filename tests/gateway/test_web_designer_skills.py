"""Behaviour contracts for the Phase 2 skill pack registry.

The registry (gateway/web_designer/skills.py) is the machine-readable
wiring between the 30-stage coordinator and the 45 audit skills
(skills/web-design/<name>/SKILL.md): the coordinator loads a stage's
skills with skill_view, and web_designer_status reports them for the
run's current stage. These tests pin the wiring, not the prose — the
authoring-standards suite owns the SKILL.md shape.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import hermes_yaml as yaml

import tools.web_designer_tool as tool
from agent.skill_utils import DEFAULT_AUTO_LOAD_SKILLS
from gateway.web_designer import skills as registry
from gateway.web_designer.skills import (
    ALL_SKILLS,
    SKILL_DIR,
    SKILL_STAGES,
    skills_for_stage,
    stages_for_skill,
)
from gateway.web_designer.store import create_run, save_run, start_run
from gateway.web_designer.types import STAGE_KEYS

REPO = Path(__file__).resolve().parents[2]
PACK = REPO / SKILL_DIR
RUN_ID = "0123456789abcdef"


def _frontmatter(path: Path) -> dict:
    content = path.read_text(encoding="utf-8")
    assert content.startswith("---"), f"{path}: SKILL.md must start with ---"
    end = content.index("\n---", 3)
    return yaml.safe_load(content[3:end])


def test_registry_covers_all_forty_five_skills():
    assert len(ALL_SKILLS) == 45
    assert len(set(ALL_SKILLS)) == 45


def test_every_registry_skill_has_a_skill_file_with_matching_stages():
    for name in ALL_SKILLS:
        skill_path = PACK / name / "SKILL.md"
        assert skill_path.is_file(), f"missing SKILL.md for {name}"
        fm = _frontmatter(skill_path)
        assert fm.get("name") == name, f"{name}: frontmatter name mismatch"
        declared = (fm.get("metadata") or {}).get("hermes") or {}
        stages = declared.get("web_designer_stages")
        assert list(stages or []) == list(SKILL_STAGES[name]), (
            f"{name}: frontmatter web_designer_stages {stages} != "
            f"registry {SKILL_STAGES[name]}"
        )


def test_no_orphan_skill_files_in_the_pack():
    on_disk = {p.parent.name for p in PACK.glob("*/SKILL.md")}
    assert on_disk == set(ALL_SKILLS), (
        f"skill files not in the registry: {sorted(on_disk - set(ALL_SKILLS))}; "
        f"registry skills without a file: {sorted(set(ALL_SKILLS) - on_disk)}"
    )


def test_every_stage_name_is_real():
    for name, stages in SKILL_STAGES.items():
        assert 1 <= len(stages) <= 3, f"{name}: 1-3 stages, got {stages}"
        for stage in stages:
            assert stage in STAGE_KEYS, f"{name}: unknown stage {stage!r}"


def test_design_stages_have_skills_and_judger_stages_have_none():
    design_stages = {
        "intake", "brief", "research", "ia", "wireframe", "copy",
        "direction", "font", "colour", "theme", "icon", "tokens",
        "components", "stack", "build", "preview", "revisions",
    }
    for stage in design_stages:
        assert skills_for_stage(stage), f"{stage}: no skill loads"
    for stage in ("judge_visual", "judge_a11y", "judge_perf",
                  "judge_content", "judge_responsive", "fix_loop",
                  "checkpoints", "delivery", "checkpointing",
                  "rollback", "routing_audit", "load_gate", "scope_freeze"):
        assert skills_for_stage(stage) == (), f"{stage}: takes no skills"


def test_stage_skill_lists_match_the_skill_side():
    for stage in STAGE_KEYS:
        expected = tuple(
            name for name in ALL_SKILLS if stage in SKILL_STAGES[name])
        assert skills_for_stage(stage) == expected


def test_headline_stage_mappings():
    assert skills_for_stage("ia") == (
        "information-architecture", "navigation-design",
        "laws-of-ux", "user-journeys")
    assert skills_for_stage("tokens") == (
        "typographic-scale", "semantic-colour", "spacing-system",
        "design-systems", "design-tokens", "dark-mode",
        "accessibility-wcag")
    assert "design-tokens" in skills_for_stage("tokens")
    assert "prototyping" in skills_for_stage("preview")
    assert skills_for_stage("revisions") == ("ab-testing",)


def test_unknown_stage_and_skill_raise():
    with pytest.raises(ValueError):
        skills_for_stage("not_a_stage")
    with pytest.raises(ValueError):
        stages_for_skill("not_a_skill")


def test_no_pack_skill_auto_loads():
    assert not (set(ALL_SKILLS) & set(DEFAULT_AUTO_LOAD_SKILLS))


@pytest.fixture
def launch_home(tmp_path, monkeypatch):
    home = tmp_path / "launch"
    home.mkdir()
    monkeypatch.setattr(
        "hermes_constants.get_routing_process_hermes_home", lambda: home)
    return home


def test_status_reports_the_current_stage_skills(launch_home):
    record = create_run(
        home=launch_home, thread_id="agent:main:telegram:dm:222",
        user_id="111", chat_id="222", run_id=RUN_ID)
    start_run(record)
    result = json.loads(tool.web_designer_status(RUN_ID))
    assert result["stage"] == "load_gate"
    assert result["skills"] == []
    record.stage = "ia"
    save_run(record)
    result = json.loads(tool.web_designer_status(RUN_ID))
    assert result["skills"] == list(skills_for_stage("ia"))
