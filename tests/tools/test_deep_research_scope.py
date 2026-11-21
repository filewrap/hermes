import shutil
import subprocess

import pytest

from tools.code_execution_tool import execute_code
from tools.file_tools import _deep_research_path_error
from tools.terminal_scope import reset_deep_research_root, set_deep_research_root
from tools.terminal_tool import _deep_research_bwrap_command


def test_file_tools_allow_only_the_active_session_root_and_block_symlink_escape(tmp_path):
    root = tmp_path / "session"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    (root / "escape").symlink_to(outside)
    token = set_deep_research_root(str(root))
    try:
        assert _deep_research_path_error("report.md", "Write") is None
        assert _deep_research_path_error(str(root / "notes.md"), "Read") is None
        blocked = _deep_research_path_error(str(outside), "Read")
        assert blocked and "private-session protection" in blocked
        assert "session-relative filename" in blocked and "No file access" in blocked
        symlink_blocked = _deep_research_path_error(str(root / "escape"), "Read")
        assert symlink_blocked and "private-session protection" in symlink_blocked
        assert "session-relative filename" in symlink_blocked
    finally:
        reset_deep_research_root(token)


def test_execute_code_is_disabled_inside_deep_research_because_its_kernel_is_unconfined(tmp_path):
    root = tmp_path / "session"
    root.mkdir()
    token = set_deep_research_root(str(root))
    try:
        result = execute_code("print('must not run')")
        assert "disabled inside deep-research sessions" in result
    finally:
        reset_deep_research_root(token)


import shutil

import pytest


@pytest.mark.skipif(not shutil.which("bwrap"), reason="bubblewrap is unavailable")
def test_research_terminal_rejects_a_workdir_outside_its_session(tmp_path):
    root = tmp_path / "session"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    token = set_deep_research_root(str(root))
    try:
        with pytest.raises(PermissionError, match="outside this session folder"):
            _deep_research_bwrap_command("pwd", str(root), str(outside))
    finally:
        reset_deep_research_root(token)


@pytest.mark.skipif(not shutil.which("bwrap"), reason="bubblewrap is unavailable")
def test_bubblewrap_exposes_only_the_session_folder_for_writes(tmp_path):
    root = tmp_path / "session"
    root.mkdir()
    outside = tmp_path / "host-only.txt"
    outside.write_text("must stay hidden", encoding="utf-8")
    token = set_deep_research_root(str(root))
    try:
        wrapped = _deep_research_bwrap_command(
            "pwd; printf safe > report.md; test ! -e /tmp/host-only.txt && echo isolated",
            str(root), str(root),
        )
        result = subprocess.run(wrapped, shell=True, capture_output=True, text=True, timeout=15, check=False)
        if result.returncode and "Operation not permitted" in result.stderr:
            pytest.skip("this runner does not permit bubblewrap user namespaces")
        assert result.returncode == 0, result.stderr
        assert "/workspace" in result.stdout
        assert "isolated" in result.stdout
        assert (root / "report.md").read_text(encoding="utf-8") == "safe"
        assert outside.read_text(encoding="utf-8") == "must stay hidden"
    finally:
        reset_deep_research_root(token)
