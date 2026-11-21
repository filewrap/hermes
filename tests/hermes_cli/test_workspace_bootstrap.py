"""Tests for hermes_cli/workspace_bootstrap.py — Hermes workspace bootstrap.

Seeding, [MISSING] markers, caps, IDENTITY.md parsing and the Runtime-name
resolution, exercised against explicit temp homes (no HERMES_HOME dependence).
"""

import os
import stat

import pytest

from agent.prompt_builder import DEFAULT_AGENT_IDENTITY
from hermes_cli.default_soul import DEFAULT_SOUL_MD
from hermes_cli.workspace_bootstrap import (
    BOOTSTRAP_MAX_CHARS,
    BOOTSTRAP_TOTAL_MAX_CHARS,
    USER_BOOTSTRAP_MAX_CHARS,
    WORKSPACE_SEEDS,
    build_workspace_bootstrap_prompt,
    effective_identity_theme,
    is_workspace_configured,
    load_agent_identity,
    parse_identity_markdown,
    publish_bootstrap_file,
    resolve_runtime_agent_name,
    seed_workspace_bootstrap_files,
)

ALL_FIVE = ("AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "BOOTSTRAP.md")


def test_seeds_carry_no_default_agent_name():
    """The port rule: no default agent name anywhere in the seeds."""
    for name, seed in WORKSPACE_SEEDS.items():
        assert seed.strip(), f"{name} seed is empty"
        assert seed.startswith(f"# {name}"), f"{name} seed lost its heading"
        assert "Hermes Agent" not in seed, f"{name} seed bakes in a default name"


def test_soul_seed_matches_the_identity_fallback():
    """Seed and fallback stay one text: fresh homes and no-SOUL.md sessions get
    the same persona."""
    assert DEFAULT_SOUL_MD == WORKSPACE_SEEDS["SOUL.md"]
    assert DEFAULT_AGENT_IDENTITY == WORKSPACE_SEEDS["SOUL.md"]


def test_fresh_home_gets_all_five_seeded(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    wrote = seed_workspace_bootstrap_files(home)
    assert wrote == {name: True for name in ALL_FIVE}
    for name in ALL_FIVE:
        path = home / name
        assert path.exists()
        assert path.read_text(encoding="utf-8") == WORKSPACE_SEEDS[name]


@pytest.mark.platforms("posix")
def test_seeded_files_are_owner_only(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    seed_workspace_bootstrap_files(home)
    for name in ALL_FIVE:
        assert stat.S_IMODE((home / name).stat().st_mode) == 0o600


def test_seeding_never_overwrites_an_existing_file(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    custom = "MY CUSTOM AGENTS\n"
    (home / "AGENTS.md").write_text(custom, encoding="utf-8")
    # AGENTS.md is not a profile file, so a customized one does not mark the
    # workspace configured; IDENTITY.md still holds its seed text.
    (home / "IDENTITY.md").write_text(WORKSPACE_SEEDS["IDENTITY.md"], encoding="utf-8")
    wrote = seed_workspace_bootstrap_files(home)
    assert wrote["AGENTS.md"] is False
    assert wrote["IDENTITY.md"] is False
    assert (home / "AGENTS.md").read_text(encoding="utf-8") == custom
    assert (home / "IDENTITY.md").read_text(encoding="utf-8") == WORKSPACE_SEEDS["IDENTITY.md"]
    assert wrote["SOUL.md"] is True


def test_empty_profile_file_counts_as_configured(tmp_path):
    """An emptied IDENTITY.md differs from its starter template, so the
    workspace counts as configured and is never re-seeded."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "IDENTITY.md").write_text("", encoding="utf-8")
    assert is_workspace_configured(home) is True
    assert seed_workspace_bootstrap_files(home) == {name: False for name in ALL_FIVE}


def test_publish_is_exclusive_create_even_for_a_dangling_symlink(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    target = home / "AGENTS.md"
    os.symlink(home / "does-not-exist", target)
    assert publish_bootstrap_file(target, "seed") is False
    assert target.is_symlink()


def test_unconfigured_home_reseeds_a_deleted_file(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    seed_workspace_bootstrap_files(home)
    (home / "IDENTITY.md").unlink()
    assert seed_workspace_bootstrap_files(home)["IDENTITY.md"] is True
    assert (home / "IDENTITY.md").read_text(encoding="utf-8") == WORKSPACE_SEEDS["IDENTITY.md"]


def test_configured_home_is_never_reseeded(tmp_path):
    """A SOUL.md the user customized marks the workspace configured: deleting
    IDENTITY.md or BOOTSTRAP.md afterwards leaves them deleted."""
    home = tmp_path / "home"
    home.mkdir()
    seed_workspace_bootstrap_files(home)
    (home / "SOUL.md").write_text("My own persona, nothing like the seed.\n", encoding="utf-8")
    assert is_workspace_configured(home) is True
    (home / "IDENTITY.md").unlink()
    (home / "BOOTSTRAP.md").unlink()
    wrote = seed_workspace_bootstrap_files(home)
    assert wrote == {name: False for name in ALL_FIVE}
    assert not (home / "IDENTITY.md").exists()
    assert not (home / "BOOTSTRAP.md").exists()


def test_unfilled_seed_is_not_configured(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    seed_workspace_bootstrap_files(home)
    assert is_workspace_configured(home) is False
    (home / "USER.md").write_text("<!-- observed: 2026-11-11 | status: active -->\n\n- Prefer concise replies.\n",
                                  encoding="utf-8")
    assert is_workspace_configured(home) is True


def test_missing_files_render_markers_except_user(tmp_path):
    """Absent AGENTS/IDENTITY/BOOTSTRAP render [MISSING]; absent USER.md is
    silently dropped, exactly as the upstream agent renders them."""
    home = tmp_path / "home"
    home.mkdir()
    prompt = build_workspace_bootstrap_prompt(home)
    for name in ("AGENTS.md", "IDENTITY.md", "BOOTSTRAP.md"):
        assert f"## {home}/{name}\n\n[MISSING] Expected at: {home}/{name}" in prompt
    assert f"## {home}/USER.md" not in prompt
    assert "[MISSING] Expected at:" in prompt


def test_bootstrap_pending_shows_while_the_file_exists(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    seed_workspace_bootstrap_files(home)
    prompt = build_workspace_bootstrap_prompt(home)
    assert "## Bootstrap Pending" in prompt
    assert "BOOTSTRAP.md below; follow before normal reply." in prompt
    (home / "BOOTSTRAP.md").unlink()
    prompt = build_workspace_bootstrap_prompt(home)
    assert "## Bootstrap Pending" not in prompt
    assert f"[MISSING] Expected at: {home}/BOOTSTRAP.md" in prompt


def test_per_file_cap_trims_with_a_marker(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "AGENTS.md").write_text("x" * 50_000, encoding="utf-8")
    prompt = build_workspace_bootstrap_prompt(home)
    section = prompt.split(f"## {home}/AGENTS.md")[1].split("\n## ")[0]
    body = section.strip()
    assert len(body) <= BOOTSTRAP_MAX_CHARS
    assert "[...truncated, read AGENTS.md for full content...]" in body


def test_user_cap_is_four_thousand(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "USER.md").write_text("u" * 10_000, encoding="utf-8")
    prompt = build_workspace_bootstrap_prompt(home)
    section = prompt.split(f"## {home}/USER.md")[1].split("\n## ")[0]
    assert len(section.strip()) <= USER_BOOTSTRAP_MAX_CHARS


def test_total_cap_bounds_all_files(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    for name in ("AGENTS.md", "IDENTITY.md", "BOOTSTRAP.md"):
        (home / name).write_text("y" * 40_000, encoding="utf-8")
    prompt = build_workspace_bootstrap_prompt(home)
    bodies = []
    for name in ("AGENTS.md", "IDENTITY.md", "BOOTSTRAP.md"):
        section = prompt.split(f"## {home}/{name}")[1].split("\n## ")[0]
        bodies.append(section.strip())
    assert sum(len(body) for body in bodies) <= BOOTSTRAP_TOTAL_MAX_CHARS


def test_identity_seed_parses_to_nothing():
    assert parse_identity_markdown(WORKSPACE_SEEDS["IDENTITY.md"]) is None


def test_identity_parse_filled_and_decorated(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "IDENTITY.md").write_text(
        "# IDENTITY.md - Who Am I?\n\n"
        "- **Name:** `musie`\n"
        "- **Creature:** Flustered Protocol Droid\n"
        "- **Vibe:** Anxious, detail-obsessed\n"
        "- **Emoji:** 🤖\n"
        "- **Avatar:** avatars/musie.png\n",
        encoding="utf-8",
    )
    identity = load_agent_identity(home)
    assert identity == {
        "name": "musie",
        "creature": "Flustered Protocol Droid",
        "vibe": "Anxious, detail-obsessed",
        "emoji": "🤖",
        "avatar": "avatars/musie.png",
    }
    assert effective_identity_theme(identity) == "Flustered Protocol Droid"


def test_identity_placeholders_are_ignored():
    content = (
        "- **Name:** *(pick something you like)*\n"
        "- **Creature:** *(AI? robot? familiar? ghost in the machine? something weirder?)*\n"
        "- **Vibe:** *(how do you come across? sharp? warm? chaotic? calm?)*\n"
        "- **Emoji:** *(your signature - pick one that feels right)*\n"
        "- **Avatar:** *(workspace-relative path, http(s) URL, or data URI)*\n"
    )
    assert parse_identity_markdown(content) is None
    assert parse_identity_markdown("- **Avatar:** *(not set yet)*\n") is None


def test_identity_theme_precedence_is_theme_then_creature_then_vibe():
    assert effective_identity_theme({"theme": "T", "creature": "C", "vibe": "V"}) == "T"
    assert effective_identity_theme({"creature": "C", "vibe": "V"}) == "C"
    assert effective_identity_theme({"vibe": "V"}) == "V"
    assert effective_identity_theme({}) is None
    assert effective_identity_theme(None) is None


def test_identity_missing_file_loads_to_none(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    assert load_agent_identity(home) is None


def test_runtime_agent_name_rules():
    assert resolve_runtime_agent_name(None) is None
    assert resolve_runtime_agent_name({}) is None
    assert resolve_runtime_agent_name({"name": "  "}) is None
    assert resolve_runtime_agent_name({"name": "musie"}) == "musie"
    assert resolve_runtime_agent_name({"name": "  two\n words  "}) == "two words"
    long_name = "n" * 200
    resolved = resolve_runtime_agent_name({"name": long_name})
    assert resolved is not None and len(resolved) == 128
