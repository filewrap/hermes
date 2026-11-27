"""Tests for hermes_cli/workspace_bootstrap.py — Hermes workspace bootstrap.

Seeding, silent skips for absent files, and caps, exercised against explicit
temp homes (no HERMES_HOME dependence). One identity only — Hermes, in
SOUL.md; there is no IDENTITY.md file and no [MISSING] markers.
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
    is_workspace_configured,
    publish_bootstrap_file,
    seed_workspace_bootstrap_files,
)

ALL_FOUR = ("AGENTS.md", "SOUL.md", "USER.md", "BOOTSTRAP.md")


def test_soul_seed_is_the_official_identity():
    """The SOUL.md seed opens with the official Hermes identity, verbatim;
    no other seed bakes in a name."""
    for name, seed in WORKSPACE_SEEDS.items():
        assert seed.strip(), f"{name} seed is empty"
        assert seed.startswith(f"# {name}"), f"{name} seed lost its heading"
    assert WORKSPACE_SEEDS["SOUL.md"].startswith(
        "# SOUL.md - Operating Core\n\nYou are Hermes Agent, built by Nous Research. "
    )
    for name, seed in WORKSPACE_SEEDS.items():
        if name != "SOUL.md":
            assert "Hermes Agent" not in seed, f"{name} seed bakes in a default name"


def test_soul_seed_matches_the_identity_fallback():
    """Seed and fallback stay one text: fresh homes and no-SOUL.md sessions get
    the same persona."""
    assert DEFAULT_SOUL_MD == WORKSPACE_SEEDS["SOUL.md"]
    assert DEFAULT_AGENT_IDENTITY == WORKSPACE_SEEDS["SOUL.md"]


def test_fresh_home_gets_all_four_seeded(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    wrote = seed_workspace_bootstrap_files(home)
    assert wrote == {name: True for name in ALL_FOUR}
    for name in ALL_FOUR:
        path = home / name
        assert path.exists()
        assert path.read_text(encoding="utf-8") == WORKSPACE_SEEDS[name]


@pytest.mark.platforms("posix")
def test_seeded_files_are_owner_only(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    seed_workspace_bootstrap_files(home)
    for name in ALL_FOUR:
        assert stat.S_IMODE((home / name).stat().st_mode) == 0o600


def test_seeding_never_overwrites_an_existing_file(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    custom = "MY CUSTOM AGENTS\n"
    (home / "AGENTS.md").write_text(custom, encoding="utf-8")
    # AGENTS.md is not a profile file, so a customized one does not mark the
    # workspace configured; USER.md still holds its seed text.
    (home / "USER.md").write_text(WORKSPACE_SEEDS["USER.md"], encoding="utf-8")
    wrote = seed_workspace_bootstrap_files(home)
    assert wrote["AGENTS.md"] is False
    assert wrote["USER.md"] is False
    assert (home / "AGENTS.md").read_text(encoding="utf-8") == custom
    assert (home / "USER.md").read_text(encoding="utf-8") == WORKSPACE_SEEDS["USER.md"]
    assert wrote["SOUL.md"] is True


def test_empty_profile_file_counts_as_configured(tmp_path):
    """An emptied USER.md differs from its starter template, so the
    workspace counts as configured and is never re-seeded."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "USER.md").write_text("", encoding="utf-8")
    assert is_workspace_configured(home) is True
    assert seed_workspace_bootstrap_files(home) == {name: False for name in ALL_FOUR}


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
    (home / "BOOTSTRAP.md").unlink()
    assert seed_workspace_bootstrap_files(home)["BOOTSTRAP.md"] is True
    assert (home / "BOOTSTRAP.md").read_text(encoding="utf-8") == WORKSPACE_SEEDS["BOOTSTRAP.md"]


def test_configured_home_is_never_reseeded(tmp_path):
    """A SOUL.md the user customized marks the workspace configured: deleting
    BOOTSTRAP.md afterwards leaves it deleted."""
    home = tmp_path / "home"
    home.mkdir()
    seed_workspace_bootstrap_files(home)
    (home / "SOUL.md").write_text("My own persona, nothing like the seed.\n", encoding="utf-8")
    assert is_workspace_configured(home) is True
    (home / "BOOTSTRAP.md").unlink()
    wrote = seed_workspace_bootstrap_files(home)
    assert wrote == {name: False for name in ALL_FOUR}
    assert not (home / "BOOTSTRAP.md").exists()


def test_unfilled_seed_is_not_configured(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    seed_workspace_bootstrap_files(home)
    assert is_workspace_configured(home) is False
    (home / "USER.md").write_text("<!-- observed: 2026-11-11 | status: active -->\n\n- Prefer concise replies.\n",
                                  encoding="utf-8")
    assert is_workspace_configured(home) is True


def test_missing_files_are_silently_dropped(tmp_path):
    """Absent files render nothing — no [MISSING] markers anywhere; an
    empty home renders an empty prompt."""
    home = tmp_path / "home"
    home.mkdir()
    assert build_workspace_bootstrap_prompt(home) == ""
    (home / "AGENTS.md").write_text("# Agents\nWork here.\n", encoding="utf-8")
    prompt = build_workspace_bootstrap_prompt(home)
    assert f"## {home}/AGENTS.md" in prompt
    assert "[MISSING]" not in prompt
    assert f"## {home}/USER.md" not in prompt
    assert f"## {home}/BOOTSTRAP.md" not in prompt


def test_unreadable_files_are_silently_dropped(tmp_path):
    """A bootstrap file that cannot be read — a directory in its place, or
    undecodable bytes — renders nothing. No markers of any kind."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "AGENTS.md").mkdir()
    (home / "BOOTSTRAP.md").write_bytes(b"\xff\xfe\x00bad bytes")
    prompt = build_workspace_bootstrap_prompt(home)
    assert prompt == ""
    assert "UNREADABLE" not in prompt
    assert "MISSING" not in prompt


def test_bootstrap_file_renders_as_plain_context(tmp_path):
    """BOOTSTRAP.md renders as an ordinary context file — no Bootstrap
    Pending block, and deleting it leaves no marker behind."""
    home = tmp_path / "home"
    home.mkdir()
    seed_workspace_bootstrap_files(home)
    prompt = build_workspace_bootstrap_prompt(home)
    assert "## Bootstrap Pending" not in prompt
    assert "follow before normal reply" not in prompt
    assert f"## {home}/BOOTSTRAP.md" in prompt
    assert "Changing How You're Invoked" in prompt
    (home / "BOOTSTRAP.md").unlink()
    prompt = build_workspace_bootstrap_prompt(home)
    assert "## Bootstrap Pending" not in prompt
    assert f"## {home}/BOOTSTRAP.md" not in prompt
    assert "[MISSING]" not in prompt


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
    for name in ("AGENTS.md", "BOOTSTRAP.md"):
        (home / name).write_text("y" * 40_000, encoding="utf-8")
    prompt = build_workspace_bootstrap_prompt(home)
    bodies = []
    for name in ("AGENTS.md", "BOOTSTRAP.md"):
        section = prompt.split(f"## {home}/{name}")[1].split("\n## ")[0]
        bodies.append(section.strip())
    assert sum(len(body) for body in bodies) <= BOOTSTRAP_TOTAL_MAX_CHARS
