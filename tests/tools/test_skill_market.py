"""Regression tests for the shared skill registry ("market") and fail-open loading.

The market is one dir at the launch root (``skills-market``) that every profile
reads as an external skills root, so a market skill is visible in a fresh group
profile that never installed anything. Loading refuses nothing on lint warnings.
"""

import importlib
import json
from pathlib import Path


def _skill_md(name: str, description: str, body_extra: str = "") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"# {name}\n\n"
        "Use when the test needs it. Does the thing.\n"
        f"{body_extra}"
    )


def _write_market_skill(launch_home: Path, name: str) -> Path:
    skill_dir = launch_home / "skills-market" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        _skill_md(name, "A market skill, visible to every profile."),
        encoding="utf-8",
    )
    return skill_dir


def test_market_skill_visible_in_fresh_group_profile(tmp_path, monkeypatch):
    """A registry skill loads in a group profile that never installed anything."""
    launch_home = tmp_path / "launch"
    group_home = tmp_path / "profiles" / "gc-123"
    market_skill_dir = _write_market_skill(launch_home, "market-skill")
    group_skills = group_home / "skills"
    group_skills.mkdir(parents=True)  # fresh group profile: empty skills dir

    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    from tools import skills_tool

    skills_tool = importlib.reload(skills_tool)
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", group_skills)

    result = json.loads(skills_tool.skill_view("market-skill", preprocess=False))

    assert result["success"] is True
    assert result["name"] == "market-skill"
    assert Path(result["skill_dir"]) == market_skill_dir
    assert "market skill" in result["content"].lower()


def test_skill_manage_create_global_lands_in_market(tmp_path, monkeypatch):
    """skill_manage create with scope='global' writes into the shared registry."""
    launch_home = tmp_path / "launch"
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    from tools.skill_manager_tool import _create_skill

    result = _create_skill(
        "market-made",
        _skill_md("market-made", "Created straight into the market."),
        scope="global",
    )

    assert result["success"] is True
    assert result["scope"] == "global"
    assert (launch_home / "skills-market" / "market-made" / "SKILL.md").is_file()


def test_install_from_quarantine_global_lands_in_market(tmp_path, monkeypatch):
    """A hub install with scope='global' lands in the market with its own lock entry."""
    launch_home = tmp_path / "launch"
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    from tools.skills_guard import ScanResult
    from tools.skills_hub import HubLockFile
    from tools.skills_hub_install import install_from_quarantine, quarantine_bundle
    from tools.skills_hub_models import SkillBundle

    content = _skill_md("hub-skill", "Installed from the hub into the market.")
    bundle = SkillBundle(
        name="hub-skill",
        files={"SKILL.md": content},
        source="github",
        identifier="owner/repo/hub-skill",
        trust_level="community",
    )
    scan = ScanResult(
        skill_name="hub-skill", source="github", trust_level="community", verdict="safe",
    )
    quarantine = quarantine_bundle(bundle)

    install_dir = install_from_quarantine(quarantine, "hub-skill", "", bundle, scan, scope="global")

    assert install_dir == launch_home / "skills-market" / "hub-skill"
    assert (install_dir / "SKILL.md").is_file()
    assert HubLockFile(launch_home / "skills-market" / ".hub" / "lock.json").get_installed(
        "hub-skill"
    ) is not None


def test_install_from_quarantine_rejects_unknown_scope(tmp_path, monkeypatch):
    """An unknown install scope fails fast instead of landing in the wrong root."""
    import pytest

    from tools.skills_guard import ScanResult
    from tools.skills_hub_install import install_from_quarantine
    from tools.skills_hub_models import SkillBundle

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    bundle = SkillBundle(
        name="hub-skill", files={}, source="github",
        identifier="owner/repo/hub-skill", trust_level="community",
    )
    scan = ScanResult(
        skill_name="hub-skill", source="github", trust_level="community", verdict="safe",
    )

    with pytest.raises(ValueError, match="scope must be"):
        install_from_quarantine(tmp_path / "q", "hub-skill", "", bundle, scan, scope="everywhere")


def test_lint_warnings_never_block_load(tmp_path, monkeypatch):
    """A skill tripping lint warnings — forbidden README.md, oversized soft-budget
    body, missing authoring metadata — still loads. Lint is advisory, never a gate."""
    root = tmp_path / "skills"
    skill_dir = root / "noisy-skill"
    skill_dir.mkdir(parents=True)
    long_body = "Do the thing, then check the result. " * 900  # ~33k chars, over the soft budget
    (skill_dir / "SKILL.md").write_text(
        _skill_md("noisy-skill", "A noisy skill. " * 20 + "Use when testing.", long_body),
        encoding="utf-8",
    )
    (skill_dir / "README.md").write_text("# readme\n", encoding="utf-8")

    from tools.skill_linter import lint_skill

    findings = lint_skill(skill_dir / "SKILL.md")
    assert findings, "the test skill must actually trip lint warnings"
    assert any(f.rule == "forbidden-file" for f in findings)

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import skills_tool

    skills_tool = importlib.reload(skills_tool)
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", root)

    result = json.loads(skills_tool.skill_view("noisy-skill", preprocess=False))

    assert result["success"] is True
    assert result["name"] == "noisy-skill"
    assert "Do the thing" in result["content"]
