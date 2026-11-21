"""Generic file tools must not bypass managed memory and skill write controls."""

import json
from unittest.mock import MagicMock

import pytest

from tools import file_tools_write_guards as guards
from tools.file_tools import patch_tool, write_file_tool


@pytest.fixture
def managed_home(tmp_path, monkeypatch):
    """Use synthetic managed directories; never touch the real user profile."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(guards, "_hermes_exempt_homes", lambda: (str(home),))
    return home


def _mock_file_ops(monkeypatch):
    from tools import file_tools

    get_ops = MagicMock()
    monkeypatch.setattr(file_tools, "_get_file_ops", get_ops)
    return get_ops


@pytest.mark.parametrize(
    "relative_path",
    [
        "skills/telegram-client/SKILL.md",
        "skills/telegram-client/references/notes.md",
        "memories/MEMORY.md",
        "memory/USER.md",
        "pending/memory/approval.json",
        "pending/skills/approval.json",
        "MEMORY.md",
        "USER.md",
    ],
)
def test_write_file_blocks_managed_memory_and_skill_paths(managed_home, monkeypatch, relative_path):
    get_ops = _mock_file_ops(monkeypatch)
    target = managed_home / relative_path

    result = json.loads(write_file_tool(str(target), "synthetic test content"))

    assert "error" in result
    assert "managed personal memory or skills" in result["error"]
    get_ops.assert_not_called()


def test_replace_patch_blocks_personal_skill_path(managed_home, monkeypatch):
    get_ops = _mock_file_ops(monkeypatch)
    target = managed_home / "skills" / "telegram-client" / "SKILL.md"

    result = json.loads(patch_tool(
        mode="replace", path=str(target), old_string="before", new_string="after",
    ))

    assert "error" in result
    assert "managed personal memory or skills" in result["error"]
    get_ops.assert_not_called()


def test_v4a_patch_header_blocks_managed_memory_path(managed_home, monkeypatch):
    get_ops = _mock_file_ops(monkeypatch)
    target = managed_home / "memories" / "MEMORY.md"
    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {target}\n"
        "@@\n-before\n+after\n"
        "*** End Patch\n"
    )

    result = json.loads(patch_tool(mode="patch", patch=patch))

    assert "error" in result
    assert "managed personal memory or skills" in result["error"]
    get_ops.assert_not_called()


def test_symlink_alias_into_managed_skills_is_blocked(managed_home, tmp_path, monkeypatch):
    get_ops = _mock_file_ops(monkeypatch)
    target = managed_home / "skills" / "telegram-client" / "SKILL.md"
    target.parent.mkdir(parents=True)
    alias = tmp_path / "external-alias.md"
    alias.symlink_to(target)

    result = json.loads(patch_tool(
        mode="replace", path=str(alias), old_string="before", new_string="after",
    ))

    assert "error" in result
    assert "managed personal memory or skills" in result["error"]
    get_ops.assert_not_called()


def test_telegram_scoped_memory_directory_is_blocked(managed_home, tmp_path, monkeypatch):
    from gateway import telegram_personal_settings

    get_ops = _mock_file_ops(monkeypatch)
    scoped_memory_dir = tmp_path / "private-scope" / "memories"
    monkeypatch.setattr(
        telegram_personal_settings, "current_personal_memory_dir", lambda: scoped_memory_dir,
    )

    result = json.loads(write_file_tool(str(scoped_memory_dir / "MEMORY.md"), "synthetic content"))

    assert "error" in result
    assert "managed personal memory or skills" in result["error"]
    get_ops.assert_not_called()
