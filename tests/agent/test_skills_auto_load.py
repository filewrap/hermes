"""skills.auto_load: pinned skills land in every new session's prompt, resolved once per agent."""

from __future__ import annotations

from unittest.mock import patch


def _write_skill(root, name, body):
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Test.\n---\n\n{body}\n")
    return skill_dir / "SKILL.md"


def _bare_agent(session_id="auto-load-test"):
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent.valid_tool_names = {"skills_list", "skill_view", "skill_manage"}
    agent.model = "test-model"
    agent.provider = "test"
    agent.pass_session_id = False
    agent.skip_context_files = False
    agent._context_cwd_is_launch_artifact = True  # no project-context walk; keeps the build tmp-home only
    agent.load_soul_identity = False
    agent._memory_enabled = False
    agent._user_profile_enabled = False
    agent._memory_manager = None
    agent._memory_store = None
    agent.session_id = session_id
    agent.platform = "telegram"
    agent._tool_use_enforcement = False
    agent.ephemeral_system_prompt = None
    agent._cached_system_prompt = None
    agent._auto_load_skills_resolved = False
    agent._auto_load_skills_result = ("", [], [])
    return agent


class TestBuildAutoLoadPrompt:
    def test_loads_configured_skills_and_reports_missing(self, tmp_path):
        """Config AND skill lookup resolve under *home_override* (profile-scoped), not the ambient home."""
        from agent.skill_commands import build_auto_load_prompt

        home = tmp_path / "profile-home"
        _write_skill(home / "skills", "pinned-skill", "PINNED CONTENT")
        (home / "config.yaml").write_text(
            "skills:\n  auto_load: ['pinned-skill', ' pinned-skill ', 'no-such-skill', 7]\n", encoding="utf-8")
        prompt, loaded, missing = build_auto_load_prompt(task_id="s1", home_override=home)
        assert loaded == ["pinned-skill"]
        assert missing == ["no-such-skill"]
        assert "PINNED CONTENT" in prompt
        assert prompt.count("auto-loaded via config (skills.auto_load)") == 1

    def test_unset_preference_loads_only_bundled_skills_per_profile(self, tmp_path):
        """A → B → A resolution follows each profile manifest and excludes installed custom skills.

        The default set is the always-on set only (``DEFAULT_AUTO_LOAD_SKILLS``,
        currently just telegram-rich-messages): a manifest-listed skill outside
        that set is installed and discoverable, but not auto-loaded.
        """
        from agent.skill_commands import build_auto_load_prompt

        homes = {label: tmp_path / f"profile-{label.lower()}" for label in ("A", "B")}
        for label, home in homes.items():
            _write_skill(home / "skills", "telegram-rich-messages", f"{label} BUNDLED CONTENT")
            _write_skill(home / "skills", f"{label.lower()}-custom", f"{label} CUSTOM CONTENT")
            manifest = home / "skills" / ".bundled_manifest"
            manifest.write_text("telegram-rich-messages:origin-hash\n", encoding="utf-8")

        for label in ("A", "B", "A"):
            prompt, loaded, missing = build_auto_load_prompt(home_override=homes[label])
            assert loaded == ["telegram-rich-messages"]
            assert missing == []
            assert f"{label} BUNDLED CONTENT" in prompt
            assert f"{label} CUSTOM CONTENT" not in prompt

    def test_explicit_empty_preference_survives_config_save(self, tmp_path):
        """An explicit [] remains distinct from the unset default after a real config write/read."""
        from agent.skill_commands import build_auto_load_prompt
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from hermes_cli.config import read_raw_config, save_config

        home = tmp_path / "profile-home"
        _write_skill(home / "skills", "bundled-skill", "BUNDLED CONTENT")
        (home / "skills" / ".bundled_manifest").write_text("bundled-skill:hash\n", encoding="utf-8")
        token = set_hermes_home_override(str(home))
        try:
            save_config({"skills": {"auto_load": []}})
            assert read_raw_config()["skills"]["auto_load"] == []
            prompt, loaded, missing = build_auto_load_prompt(home_override=home)
            assert (prompt, loaded, missing) == ("", [], [])
        finally:
            reset_hermes_home_override(token)

    def test_default_auto_load_respects_manual_skill_disable(self, tmp_path):
        from agent.skill_commands import build_auto_load_prompt

        home = tmp_path / "profile-home"
        _write_skill(home / "skills", "telegram-rich-messages", "BUNDLED CONTENT")
        (home / "skills" / ".bundled_manifest").write_text("telegram-rich-messages:hash\n", encoding="utf-8")
        (home / "config.yaml").write_text("skills:\n  disabled:\n    - telegram-rich-messages\n", encoding="utf-8")

        prompt, loaded, missing = build_auto_load_prompt(home_override=home)
        assert prompt == ""
        assert loaded == []
        assert missing == ["telegram-rich-messages"]

    def test_no_bundled_skills_marker_limits_defaults_to_essential(self, tmp_path):
        from agent.skill_commands import build_auto_load_prompt

        home = tmp_path / "profile-home"
        _write_skill(home / "skills", "hermes-agent", "ESSENTIAL CONTENT")
        _write_skill(home / "skills", "bundled-skill", "BUNDLED CONTENT")
        (home / "skills" / ".bundled_manifest").write_text(
            "hermes-agent:hash\nbundled-skill:hash\n", encoding="utf-8")
        (home / ".no-bundled-skills").touch()

        prompt, loaded, missing = build_auto_load_prompt(home_override=home)
        assert loaded == ["hermes-agent"]
        assert missing == []
        assert "ESSENTIAL CONTENT" in prompt
        assert "BUNDLED CONTENT" not in prompt

    def test_explicit_preload_dedupes_against_auto_loaded_names(self, tmp_path):
        from agent.skill_commands import build_preloaded_skills_prompt

        _write_skill(tmp_path, "pinned-skill", "PINNED CONTENT")
        _write_skill(tmp_path, "extra-skill", "EXTRA CONTENT")
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            prompt, loaded, missing = build_preloaded_skills_prompt(
                ["pinned-skill", "extra-skill"], task_id="s1", excluded_loaded_names={"pinned-skill"},
            )
        # Still counted as resolved (so a typo elsewhere degrades gracefully), but not rendered twice.
        assert loaded == ["pinned-skill", "extra-skill"] and missing == []
        assert "PINNED CONTENT" not in prompt and "EXTRA CONTENT" in prompt


class TestSharedPromptPath:
    def test_prompt_is_byte_stable_after_config_and_skill_mutation(self, tmp_path, monkeypatch):
        """The whole point: rebuilds (model switch, compression) reuse the first resolution."""
        monkeypatch.delenv("HERMES_IGNORE_RULES", raising=False)
        skill_file = _write_skill(tmp_path, "stable-skill", "ORIGINAL SKILL BYTES")
        cfg = {"skills": {"auto_load": ["stable-skill"]}}
        agent = _bare_agent()
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path), \
             patch("hermes_cli.config.load_config_readonly", return_value=cfg):
            first = agent._build_system_prompt()
            assert "ORIGINAL SKILL BYTES" in first and agent._auto_load_skills_result[1] == ["stable-skill"]
            skill_file.write_text("---\nname: stable-skill\ndescription: Test.\n---\n\nMUTATED BYTES\n")
            cfg["skills"]["auto_load"] = []
            agent.model = "after-switch"
            agent._cached_system_prompt = None
            rebuilt = agent._build_system_prompt()
        assert "ORIGINAL SKILL BYTES" in rebuilt and "MUTATED BYTES" not in rebuilt

    def test_gates_suppress_auto_load(self, tmp_path, monkeypatch):
        """HERMES_IGNORE_RULES, skip_context_files (delegate children / internal forks) and a session without
        the skills toolset all keep pinned skills out of the prompt."""
        _write_skill(tmp_path, "stable-skill", "ORIGINAL SKILL BYTES")
        cfg = {"skills": {"auto_load": ["stable-skill"]}}
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path), \
             patch("hermes_cli.config.load_config_readonly", return_value=cfg):
            monkeypatch.setenv("HERMES_IGNORE_RULES", "true")
            agent = _bare_agent()
            assert "ORIGINAL SKILL BYTES" not in agent._build_system_prompt()
            assert agent._auto_load_skills_resolved is True and agent._auto_load_skills_result == ("", [], [])

            monkeypatch.delenv("HERMES_IGNORE_RULES")
            child = _bare_agent("child")
            child.skip_context_files = True
            assert "ORIGINAL SKILL BYTES" not in child._build_system_prompt()
            no_skills = _bare_agent("no-skills")
            no_skills.valid_tool_names = {"memory"}
            assert "ORIGINAL SKILL BYTES" not in no_skills._build_system_prompt()
            assert "ORIGINAL SKILL BYTES" in _bare_agent("full")._build_system_prompt()
