"""Real profile/user skill resolution exercised in A -> B -> A order."""
import json

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from gateway.telegram_personal_settings import set_personal_memory_context, reset_personal_memory_context, save_personal_settings
from tools.skills_tool import skills_list, skill_view
from agent.skill_commands import (
    build_skill_invocation_message, get_skill_commands, personal_skill_command_alias,
    resolve_auto_load_skills,
)


def _skill(root, name, body):
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {name} desc\n---\n{body}\n", encoding="utf-8")


def test_telegram_personal_skills_isolate_profiles_a_b_a(tmp_path):
    homes = {"A": tmp_path / "profile-a", "B": tmp_path / "profile-b"}
    for label, uid, private in (("A", "101", "alice-only"), ("B", "202", "bob-only")):
        home = homes[label]
        _skill(home / "skills", "shared-only", f"{label} profile content")
        _skill(home / "skills", "collision", f"{label} profile collision wins")
        _skill(home / f"personal/telegram/users/{uid}/skills", private, f"{label} private")
        _skill(home / f"personal/telegram/users/{uid}/skills", "collision", f"{label} private collision")
        save_personal_settings(home, uid, {"selected_skills": [private]})
    home_token = set_hermes_home_override(homes["A"])
    try:
        def inspect(label, uid, expected, absent):
            set_hermes_home_override(homes[label])
            ctx = set_personal_memory_context(homes[label], uid)
            try:
                listed = json.loads(skills_list())
                names = {item["name"] for item in listed["skills"]}
                assert expected in names
                assert absent not in names
                assert "shared-only" in names
                commands = get_skill_commands()
                assert f"/{expected}" in commands
                assert f"/{absent}" not in commands
                alias = "/" + personal_skill_command_alias("collision")
                assert alias in commands
                private_message = build_skill_invocation_message(alias, "use my private copy")
                assert f"{label} private collision" in private_message
                assert "profile collision wins" not in private_message
                profile_message = build_skill_invocation_message("/collision", "use the profile copy")
                assert f"{label} profile collision wins" in profile_message
                assert f"{label} private collision" not in profile_message
                assert resolve_auto_load_skills({"skills": {}}) == [expected]
                from agent.prompt_builder import build_skills_system_prompt
                from agent.skill_commands import build_auto_load_prompt
                index = build_skills_system_prompt(skills_dir_override=homes[label] / "skills")
                assert expected in index and absent not in index
                prompt, loaded, missing = build_auto_load_prompt(home_override=homes[label])
                assert expected in loaded and not missing
                assert f"{label} private" in prompt and absent not in prompt
                viewed = json.loads(skill_view(expected))
                assert viewed["success"]
                assert f"{label} private" in viewed["content"]
                # Bare-name lookup follows tier precedence (project > local >
                # create_dir > external): the profile copy wins and the
                # private copy is shadowed, never silently loaded. The
                # private copy stays reachable via its alias (asserted above).
                resolved = json.loads(skill_view("collision"))
                assert resolved["success"]
                assert f"{label} profile collision wins" in resolved["content"]
                assert f"{label} private collision" not in resolved["content"]
                outside = tmp_path / "outside" / "SKILL.md"
                outside.parent.mkdir(exist_ok=True)
                outside.write_text("---\nname: collision\ndescription: outside\n---\nOUTSIDE\n")
                blocked = json.loads(skill_view("collision", _resolved_skill_path=str(outside)))
                assert not blocked["success"] and "outside the active skill context" in blocked["error"]
            finally:
                reset_personal_memory_context(ctx)
        inspect("A", "101", "alice-only", "bob-only")
        inspect("B", "202", "bob-only", "alice-only")
        inspect("A", "101", "alice-only", "bob-only")
    finally:
        reset_hermes_home_override(home_token)
