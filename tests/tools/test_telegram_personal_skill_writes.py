"""Real skill_manage mutations are confined to the active Telegram user's private root.

The profile owner is exempt: the owner's DM falls back to the global
lookup, so patching a bundled or profile skill the owner can see in
skills_list works instead of failing with "not found in active profile".
"""
import json

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from gateway.telegram_personal_settings import set_personal_memory_context, reset_personal_memory_context
from tools.skill_manager_tool import skill_manage


def _skill(root, name, body="original body"):
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {name} description\n---\n{body}\n", encoding="utf-8")
    return path


def _content(body):
    return f"---\nname: profile-skill\ndescription: profile description\n---\n{body}\n"


def test_skill_manage_writes_are_private_across_profiles_a_b_a(tmp_path):
    home = tmp_path / "profile"
    shared = _skill(home / "skills", "shared-skill")
    alice = _skill(home / "personal/telegram/users/101/skills", "alice-skill", "alice original")
    bob = _skill(home / "personal/telegram/users/202/skills", "bob-skill", "bob original")
    other_shared = shared.read_text(encoding="utf-8")
    home_token = set_hermes_home_override(home)
    try:
        def run_as(uid, action, **kwargs):
            context = set_personal_memory_context(home, uid)
            try:
                return json.loads(skill_manage(action=action, **kwargs))
            finally:
                reset_personal_memory_context(context)

        # A's private skill can be edited and supported files written, but shared is not a target.
        assert run_as("101", "edit", name="alice-skill", content=_content("alice edited"))["success"]
        assert "alice edited" in alice.read_text(encoding="utf-8")
        assert not run_as("101", "edit", name="shared-skill", content=_content("tampered"))["success"]
        assert not run_as("101", "patch", name="shared-skill", old_string="original body", new_string="tampered")["success"]
        assert not run_as("101", "write_file", name="shared-skill", file_path="references/x.md", file_content="tampered")["success"]
        assert not run_as("101", "remove_file", name="shared-skill", file_path="references/x.md")["success"]
        assert not run_as("101", "delete", name="shared-skill")["success"]
        assert shared.read_text(encoding="utf-8") == other_shared
        assert not (shared.parent / "references").exists()

        # Create writes go to A's personal root, not the profile root; then B cannot find A's skill.
        created = run_as("101", "create", name="alice-created", content="---\nname: alice-created\ndescription: Create Alice private skill.\n---\nbody\n")
        assert created["success"]
        assert (home / "personal/telegram/users/101/skills/alice-created/SKILL.md").exists()
        assert not run_as("202", "edit", name="alice-skill", content=_content("cross-user"))["success"]
        assert not run_as("202", "delete", name="alice-created")["success"]
        assert "alice edited" in alice.read_text(encoding="utf-8")
        assert "bob original" in bob.read_text(encoding="utf-8")

        # Return to A after B to catch stale/cached identity resolution.
        assert run_as("101", "patch", name="alice-skill", old_string="alice edited", new_string="alice final")["success"]
        assert "alice final" in alice.read_text(encoding="utf-8")
        assert shared.read_text(encoding="utf-8") == other_shared
    finally:
        reset_hermes_home_override(home_token)


def test_owner_dm_falls_back_to_global_lookup_for_shared_skills(tmp_path):
    """Owner patch of a shared skill works; a non-owner still cannot touch it."""
    home = tmp_path / "profile"
    shared = _skill(home / "skills", "shared-skill")
    (home / ".env").write_text("OWNER_TELEGRAM_ID=101\n", encoding="utf-8")
    other_shared = shared.read_text(encoding="utf-8")
    home_token = set_hermes_home_override(home)
    try:
        def run_as(uid, action, **kwargs):
            context = set_personal_memory_context(home, uid)
            try:
                return json.loads(skill_manage(action=action, **kwargs))
            finally:
                reset_personal_memory_context(context)

        # The owner patches the shared skill in place; the file changes.
        assert run_as("101", "patch", name="shared-skill", old_string="original body", new_string="owner patched")["success"]
        assert "owner patched" in shared.read_text(encoding="utf-8")
        assert "original body" not in shared.read_text(encoding="utf-8")

        # A non-owner still cannot patch the same shared skill.
        assert not run_as("202", "patch", name="shared-skill", old_string="owner patched", new_string="tampered")["success"]
        assert "owner patched" in shared.read_text(encoding="utf-8")
        assert shared.read_text(encoding="utf-8") != other_shared
    finally:
        reset_hermes_home_override(home_token)


def test_owner_dm_without_owner_env_stays_confined(tmp_path):
    """No OWNER_TELEGRAM_ID in the profile .env: the owner check fails closed."""
    home = tmp_path / "profile"
    _skill(home / "skills", "shared-skill")
    home_token = set_hermes_home_override(home)
    try:
        context = set_personal_memory_context(home, "101")
        try:
            result = json.loads(skill_manage(action="patch", name="shared-skill", old_string="original body", new_string="tampered"))
        finally:
            reset_personal_memory_context(context)
        assert not result["success"]
    finally:
        reset_hermes_home_override(home_token)
