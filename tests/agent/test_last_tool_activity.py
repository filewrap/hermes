"""Last-activity record: the tool executor stamps the last completed tool call
+ response preview on the agent, ``get_activity_summary`` exposes it, and
``format_last_tool_activity`` renders it for /last and for the
no-visible-answer turn notice. The rendering is a professional summary
(tool, age, duration, outcome, short ``key: value`` pairs) — never a raw
JSON dump; internal content still shows as recorded (secrets redacted at
record time); exposure cleanup is a later pass.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _activity(**overrides):
    base = {
        "tool": "terminal",
        "args": '{"command": "ls /tmp"}',
        "result": "a.txt\nb.txt",
        "error": False,
        "blocked": False,
        "duration_s": 1.2,
        "ts": 1000.0,
    }
    base.update(overrides)
    return base


class TestFormatLastToolActivity:
    def test_empty_or_missing_activity_renders_nothing(self):
        from agent.session_activity import format_last_tool_activity

        assert format_last_tool_activity(None) is None
        assert format_last_tool_activity({}) is None
        assert format_last_tool_activity({"tool": "  "}) is None

    def test_full_activity_renders_tool_args_result_and_age(self):
        from agent.session_activity import format_last_tool_activity

        out = format_last_tool_activity(_activity(), now=1012.0)
        assert out is not None
        assert "Last activity: `terminal`" in out
        assert "12s ago · 1.2s · Completed" in out
        assert "Arguments: command: ls /tmp" in out
        assert "Result: a.txt b.txt" in out
        # Professional summary: no raw JSON dump, no escaped quotes.
        assert "Args:" not in out
        assert '{"command"' not in out

    def test_json_args_and_results_render_as_short_key_value_pairs(self):
        from agent.session_activity import format_last_tool_activity

        out = format_last_tool_activity(_activity(
            tool="write_file",
            args='{"content": "<h1>Hello</h1>", "path": "/root/page.html"}',
            result='{"bytes_written": 399, "verified": true, "resolved_path": "/root/page.html"}',
            duration_s=0.1,
        ), now=1003.0)
        assert out is not None
        assert "Last activity: `write_file`" in out
        assert "3s ago · 0.1s · Completed" in out
        assert "Arguments: content: <h1>Hello</h1> · path: /root/page.html" in out
        assert ("Result: bytes_written: 399 · verified: true · "
                "resolved_path: /root/page.html") in out

    def test_long_values_collapse_to_preview_with_length(self):
        from agent.session_activity import format_last_tool_activity

        out = format_last_tool_activity(_activity(
            args='{"content": "' + "x" * 500 + '"}',
            result="y" * 500,
        ), now=1001.0)
        assert out is not None
        assert "(500 chars)" in out
        assert "x" * 500 not in out
        assert "y" * 500 not in out

    def test_many_fields_are_capped_with_a_remainder_count(self):
        from agent.session_activity import format_last_tool_activity

        import json

        out = format_last_tool_activity(_activity(
            result=json.dumps({f"k{i}": i for i in range(8)}),
        ), now=1001.0)
        assert out is not None
        assert "… +3 more" in out
        assert "k7" not in out

    def test_error_and_blocked_flags_are_visible(self):
        from agent.session_activity import format_last_tool_activity

        out = format_last_tool_activity(_activity(error=True, result="boom"), now=1001.0)
        assert "Result (error): boom" in out
        out = format_last_tool_activity(_activity(blocked=True, result="denied"), now=1001.0)
        assert "Blocked by a guardrail" in out


class TestBuildLastToolActivity:
    def test_truncates_long_args_and_results(self):
        from agent.tool_executor import _build_last_tool_activity

        record = _build_last_tool_activity(
            "read_file",
            {"path": "/x/" + "y" * 500},
            "r" * 5000,
            duration_s=2.345, is_error=False, blocked=False,
        )
        assert record["tool"] == "read_file"
        assert len(record["args"]) <= 303  # 300 + "..."
        assert record["args"].endswith("...")
        assert len(record["result"]) <= 2003
        assert record["result"].endswith("...")
        assert record["duration_s"] == 2.3
        assert record["error"] is False and record["blocked"] is False
        assert isinstance(record["ts"], float)

    def test_non_string_results_are_kept_readable(self):
        from agent.tool_executor import _build_last_tool_activity

        record = _build_last_tool_activity(
            "search", {"q": "x"}, {"hits": 3}, duration_s=0.5, is_error=True, blocked=False,
        )
        assert '"hits": 3' in record["result"]
        assert record["error"] is True


@pytest.fixture()
def loop_agent():
    """AIAgent with a mocked OpenAI client (mirrors test_run_agent's fixture)."""
    from run_agent import AIAgent
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.tool_delay = 0
        agent.compression_enabled = False
        agent.save_trajectories = False
        return agent


def test_activity_summary_exposes_last_tool_activity(loop_agent):
    loop_agent._last_tool_activity = _activity()
    summary = loop_agent.get_activity_summary()
    assert summary["last_tool_activity"]["tool"] == "terminal"
    assert summary["last_tool_activity"]["result"] == "a.txt\nb.txt"


def test_activity_summary_defaults_to_no_last_tool_activity(loop_agent):
    assert loop_agent.get_activity_summary()["last_tool_activity"] is None
