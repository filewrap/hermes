"""Tests for the ``transcripts`` tool (F12): list/show/transcribe/status/import.

The tool is a thin wrapper over the existing STT engine
(``tools/transcription_tools.py``) and the stored Meet captures under
``<HERMES_HOME>/workspace/meetings/``. All calls go through real registry
dispatch; no LLM calls. The STT engine is never asked to do real work here:
``transcribe`` is exercised on a missing file (engine validation rejects it
before any provider is touched).
"""

import json
import os
import time

import pytest

import tools.transcripts_tool  # import registers the tool
from tools.registry import registry
from toolsets import TOOLSETS, _HERMES_CORE_TOOLS


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _call(args):
    result = registry.dispatch("transcripts", args)
    assert isinstance(result, str)
    return json.loads(result)


def _seed_capture(home, meeting_id, lines, mtime=None):
    capture_dir = home / "workspace" / "meetings" / meeting_id
    capture_dir.mkdir(parents=True, exist_ok=True)
    path = capture_dir / "transcript.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# =========================================================================
# Registration
# =========================================================================


class TestRegistration:
    def test_registered_in_transcripts_toolset(self):
        entry = registry.get_entry("transcripts")
        assert entry is not None
        assert entry.toolset == "transcripts"
        assert entry.schema["name"] == "transcripts"

    def test_core_tool_and_toolset_entry(self):
        assert "transcripts" in _HERMES_CORE_TOOLS
        assert "transcripts" in TOOLSETS
        assert TOOLSETS["transcripts"]["tools"] == ["transcripts"]

    def test_schema_actions(self):
        action = tools.transcripts_tool.TRANSCRIPTS_SCHEMA["parameters"]["properties"][
            "action"
        ]
        assert action["enum"] == ["list", "show", "transcribe", "status", "import"]
        assert tools.transcripts_tool.TRANSCRIPTS_SCHEMA["parameters"]["required"] == [
            "action"
        ]


# =========================================================================
# list
# =========================================================================


class TestList:
    def test_empty_home_lists_nothing(self, home):
        result = _call({"action": "list"})
        assert result["success"] is True
        assert result["captures"] == []
        assert result["count"] == 0

    def test_lists_captures_newest_first(self, home):
        now = time.time()
        _seed_capture(home, "abc-older", ["hello"], mtime=now - 100)
        _seed_capture(home, "def-newer", ["first", "second"], mtime=now)
        result = _call({"action": "list"})
        assert [c["meeting_id"] for c in result["captures"]] == [
            "def-newer",
            "abc-older",
        ]
        newer = result["captures"][0]
        assert newer["lines"] == 2
        assert newer["active"] is False
        assert newer["path"].endswith("def-newer/transcript.txt")

    def test_limit_is_honoured(self, home):
        for i in range(5):
            _seed_capture(home, f"m{i}", ["x"])
        result = _call({"action": "list", "limit": 2})
        assert result["count"] == 2

    def test_active_meeting_is_flagged(self, home):
        _seed_capture(home, "abc-live", ["hello"])
        active = home / "workspace" / "meetings" / ".active.json"
        active.write_text(
            json.dumps({"meeting_id": "abc-live", "pid": 1}), encoding="utf-8"
        )
        result = _call({"action": "list"})
        assert result["captures"][0]["active"] is True


# =========================================================================
# show
# =========================================================================


class TestShow:
    def test_reads_capture_back_verbatim(self, home):
        lines = ["[00:01] Ana: hello there", "[00:02] Bo: hi Ana"]
        _seed_capture(home, "abc-123", lines)
        result = _call({"action": "show", "meeting_id": "abc-123"})
        assert result["success"] is True
        assert result["text"] == "\n".join(lines)
        assert result["lines"] == lines
        assert result["total_lines"] == 2
        assert result["truncated"] is False

    def test_last_n_lines(self, home):
        _seed_capture(home, "abc-123", ["one", "two", "three"])
        result = _call({"action": "show", "meeting_id": "abc-123", "last": 1})
        assert result["lines"] == ["three"]
        assert result["total_lines"] == 3

    def test_show_without_id_reads_active_capture(self, home):
        _seed_capture(home, "abc-live", ["active words"])
        active = home / "workspace" / "meetings" / ".active.json"
        active.write_text(
            json.dumps({"meeting_id": "abc-live", "pid": 1}), encoding="utf-8"
        )
        result = _call({"action": "show"})
        assert result["success"] is True
        assert result["text"] == "active words"

    def test_show_without_id_or_active_errors(self, home):
        result = _call({"action": "show"})
        assert result["success"] is False
        assert "list" in result["error"]

    def test_show_unknown_id_errors(self, home):
        result = _call({"action": "show", "meeting_id": "no-such-meeting"})
        assert result["success"] is False
        assert "no-such-meeting" in result["error"]


# =========================================================================
# import
# =========================================================================


class TestImport:
    def test_import_then_list_then_show_roundtrip(self, home):
        text = "Speaker A: first line\nSpeaker B: second line"
        imported = _call(
            {"action": "import", "transcript": text, "meeting_id": "weekly sync"}
        )
        assert imported["success"] is True
        assert imported["meeting_id"] == "imported-weekly-sync"
        assert imported["lines"] == 2
        listed = _call({"action": "list"})
        assert [c["meeting_id"] for c in listed["captures"]] == ["imported-weekly-sync"]
        shown = _call({"action": "show", "meeting_id": "imported-weekly-sync"})
        assert shown["text"] == text

    def test_import_refuses_to_overwrite(self, home):
        _call({"action": "import", "transcript": "first", "meeting_id": "dup"})
        result = _call(
            {"action": "import", "transcript": "second", "meeting_id": "dup"}
        )
        assert result["success"] is False
        assert "already exists" in result["error"]
        shown = _call({"action": "show", "meeting_id": "imported-dup"})
        assert shown["text"] == "first"

    def test_import_requires_text(self, home):
        result = _call({"action": "import", "transcript": "   "})
        assert result["success"] is False


# =========================================================================
# transcribe / status / dispatch errors
# =========================================================================


class TestTranscribeAndStatus:
    def test_transcribe_requires_file(self, home):
        result = _call({"action": "transcribe"})
        assert result["success"] is False
        assert "file is required" in result["error"]

    def test_transcribe_missing_file_is_engine_error(self, home):
        # The engine's own validation rejects the path before any provider,
        # model load, or network is touched.
        result = _call({"action": "transcribe", "file": "/nonexistent/voice.ogg"})
        assert result["success"] is False
        assert result["error"]

    def test_status_reports_provider_shape(self, home):
        result = _call({"action": "status"})
        assert result["success"] is True
        assert "provider" in result
        assert "enabled" in result
        assert result["active_capture"] is None

    def test_unknown_action_errors(self, home):
        result = _call({"action": "explode"})
        assert result["success"] is False
        assert "list, show, transcribe, status, or import" in result["error"]
