"""Prompt diet: skills-catalog budget, total system-prompt ceiling.

A fresh session's fixed cost has to stay small enough for a small model to
carry. The skills catalog is names-led under a fixed small budget (full
descriptions and bodies stay reachable via skills_list / skill_view), and the
built system prompt has a total ceiling that truncates the lowest-priority
sections first — never refusing or failing the turn. Identity (SOUL.md) is
never cut; everything cut stays recoverable with read_file or the skills
tools.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Fixed diet budgets (chars) — the contract values, not snapshots of a
# particular home: a 200-skill home must fit them.
SKILLS_CATALOG_BUDGET_CHARS = 16_000
SYSTEM_PROMPT_CEILING_CHARS = 48_000


def _write_skill(skills_dir, category, name, description, body_chars=0):
    d = skills_dir / category / name
    d.mkdir(parents=True, exist_ok=True)
    body = (f"\n\n{'Skill body. ' * ((body_chars // 13) + 1)}" if body_chars else "")
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\nDo the {name} workflow.{body}\n",
        encoding="utf-8",
    )
    return d


def _many_skills(home, count, description_words=28):
    skills_dir = home / "skills"
    for i in range(count):
        _write_skill(
            skills_dir, f"cat-{i % 10:02d}", f"skill-{i:03d}",
            " ".join(["descriptive phrase"] * description_words),
        )
    return skills_dir


def _fake_agent(home, tool_names, session_id="20261112_024756_4d55a452"):
    tools = [
        {"function": {"name": n, "description": f"Does {n}."}}
        for n in sorted(tool_names)
    ]
    return SimpleNamespace(
        load_soul_identity=True,
        skip_context_files=False,
        valid_tool_names=set(tool_names),
        tools=tools,
        model="upstage/solar-mini4:free",
        provider="nous",
        platform="telegram",
        session_id=session_id,
        pass_session_id=True,
        _memory_store=SimpleNamespace(format_for_system_prompt=lambda kind: ""),
        _memory_enabled=True,
        _user_profile_enabled=True,
        _memory_manager=None,
        _session_db=SimpleNamespace(
            db_path=str(home / "state.db"),
            get_session_title=lambda sid: None,
            get_conversation_root=lambda sid: None,
        ),
        _user_turn_count=1,
        _kanban_worker_guidance=None,
        _environment_probe=True,
        _bot_mode_protocol=True,
        _session_title_hint="",
        _platform_hint_overrides=None,
        _emit_diagnostic_status=lambda msg: None,
        _cached_system_prompt=None,
        context_compressor=SimpleNamespace(context_length=524288),
    )


class TestSkillsCatalogBudget:
    def test_catalog_fits_fixed_budget_names_never_dropped(self, monkeypatch, tmp_path):
        """A 120-skill catalog with long descriptions fits the fixed budget; every
        skill name stays listed (discovery survives) even when descriptions go."""
        from agent import prompt_builder as pb

        assert pb.SKILLS_CATALOG_MAX_CHARS == SKILLS_CATALOG_BUDGET_CHARS
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        skills_dir = _many_skills(tmp_path, 120)
        result = pb.build_skills_system_prompt(
            skills_dir_override=skills_dir, context_token_budget=524288)
        assert "<available_skills>" in result
        assert len(result) <= SKILLS_CATALOG_BUDGET_CHARS
        for i in range(120):
            assert f"skill-{i:03d}" in result

    def test_huge_catalog_drops_locations_before_names(self, monkeypatch, tmp_path):
        """A 250-skill catalog still fits the budget: locations go before names,
        so the model keeps the full name list and resolves locations on demand
        (skill_view / skills_list)."""
        from agent import prompt_builder as pb

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        skills_dir = _many_skills(tmp_path, 250)
        result = pb.build_skills_system_prompt(
            skills_dir_override=skills_dir, context_token_budget=524288)
        assert len(result) <= SKILLS_CATALOG_BUDGET_CHARS
        assert "<location>" not in result
        for i in range(250):
            assert f"skill-{i:03d}" in result


class TestSystemPromptCeiling:
    def _oversized_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "SOUL.md").write_text("# Test Soul\nYou are TestBot.\n", encoding="utf-8")
        (tmp_path / "USER.md").write_text("Call me Tester.\n", encoding="utf-8")
        (tmp_path / "IDENTITY.md").write_text("Name: TestBot\n", encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text(
            "# Home AGENTS\n\n" + "Home workspace rule. " * 250, encoding="utf-8")
        cwd_dir = tmp_path / "project"
        cwd_dir.mkdir()
        monkeypatch.setenv("TERMINAL_CWD", str(cwd_dir))
        (cwd_dir / "AGENTS.md").write_text(
            "# Project AGENTS\n\n" + "## Section\nProject instruction line.\n" * 1200,
            encoding="utf-8")
        _many_skills(tmp_path, 120)
        _write_skill(tmp_path / "skills", "general", "fat-skill",
                     "Auto-loaded fat skill.", body_chars=20_000)
        (tmp_path / "config.yaml").write_text(
            "skills:\n  auto_load: [fat-skill]\n", encoding="utf-8")
        return tmp_path

    def test_ceiling_binds_oversized_prompt_identity_intact(self, monkeypatch, tmp_path):
        """An oversized fresh prompt (big context files, fat auto-load, huge
        ephemeral text, 120-skill catalog) still builds under the ceiling,
        keeps SOUL.md verbatim, and is byte-identical on rebuild."""
        from agent import system_prompt as sp

        assert sp.SYSTEM_PROMPT_MAX_CHARS == SYSTEM_PROMPT_CEILING_CHARS
        home = self._oversized_home(monkeypatch, tmp_path)
        agent = _fake_agent(home, {
            "terminal", "read_file", "write_file", "memory", "memory_search",
            "memory_get", "todo_list", "skills_list", "skill_view",
            "skill_manage", "clarify", "execute_code", "delegate_task",
            "web_search", "web_extract", "patch", "search_files",
            "manage_connections",
        })
        ephemeral = "ephemeral instructions " * 2000
        parts = sp.build_system_prompt_parts(agent, system_message=ephemeral)
        full = "\n\n".join(p for p in (parts["stable"], parts["context"], parts["volatile"]) if p)
        assert len(full) <= SYSTEM_PROMPT_CEILING_CHARS
        assert "You are TestBot." in full
        parts2 = sp.build_system_prompt_parts(agent, system_message=ephemeral)
        full2 = "\n\n".join(p for p in (parts2["stable"], parts2["context"], parts2["volatile"]) if p)
        assert full == full2

    def test_ceiling_cuts_lowest_priority_first(self, monkeypatch, tmp_path):
        """Under ceiling pressure the tools-availability block (redundant with
        the tool schemas) is dropped before project context text is touched,
        and the cut is marked, not silent."""
        from agent import system_prompt as sp

        home = self._oversized_home(monkeypatch, tmp_path)
        agent = _fake_agent(home, {
            "terminal", "read_file", "write_file", "memory", "memory_search",
            "memory_get", "todo_list", "skills_list", "skill_view",
            "skill_manage", "clarify", "execute_code", "delegate_task",
            "web_search", "web_extract", "patch", "search_files",
            "manage_connections",
        })
        ephemeral = "ephemeral instructions " * 2000
        parts = sp.build_system_prompt_parts(agent, system_message=ephemeral)
        full = "\n\n".join(p for p in (parts["stable"], parts["context"], parts["volatile"]) if p)
        assert len(full) <= SYSTEM_PROMPT_CEILING_CHARS
        assert "## Available tools" not in full
        assert "You are TestBot." in full
        assert "Project instruction line." in full
        assert "truncated" in full.lower()
