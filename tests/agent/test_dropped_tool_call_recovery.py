"""No-visible-answer turns end with a last-activity notice instead of re-prompting.

Some providers (observed: claude-opus-4.8 / claude-sonnet-4.5 on GitHub
Copilot, ~2026-07) return ``finish_reason="tool_calls"`` while the parsed
``tool_calls`` array is empty — the model signalled it wanted to act but the
payload shipped no call. Other times the model returns only internal
tool-protocol markup as final text. The old fix re-prompted up to 3 times
("re-prompting (1/3)") to recover a visible answer; that loop is removed.
Now the turn ends on the first occurrence and the final answer says what
happened — plus the last tool call and its response when one was recorded
(see /last). A genuine ``finish_reason="stop"`` text turn is unaffected.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def loop_agent():
    """AIAgent with a mocked OpenAI client (mirrors test_run_agent's fixture)
    so we can stage a dropped-tool-call response + continuation pair on
    ``.chat.completions.create``."""
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


def _dropped_tool_call_response(content: str):
    """A response whose finish_reason claims a tool call, but tool_calls is
    empty — the provider contract violation the old recovery re-prompted for."""
    from tests.agent.test_run_agent import _mock_assistant_msg
    return SimpleNamespace(
        id="chatcmpl-dropped",
        model="test/model",
        choices=[SimpleNamespace(
            index=0,
            message=_mock_assistant_msg(content=content, tool_calls=None),
            finish_reason="tool_calls",
        )],
        usage=None,
    )


def _run(loop_agent, *responses):
    loop_agent.client.chat.completions.create.side_effect = list(responses)
    with (
        patch.object(loop_agent, "_persist_session"),
        patch.object(loop_agent, "_save_trajectory"),
        patch.object(loop_agent, "_cleanup_task_resources"),
    ):
        return loop_agent.run_conversation("continue the task")


class TestNoVisibleAnswerEndsWithLastActivity:
    def test_protocol_only_final_text_ends_in_one_call_with_last_activity(self, loop_agent):
        """Only-internal-markup final text ends the turn at once; the notice
        names the last tool call and its response instead of re-prompting."""
        from tests.agent.test_run_agent import _mock_response

        loop_agent._last_tool_activity = {
            "tool": "terminal", "args": '{"command": "ls"}', "result": "a.txt",
            "error": False, "blocked": False, "duration_s": 0.5, "ts": 1000.0,
        }
        protocol_only = "<|tool_call:start|>terminal<|tool_call:end|>"
        result = _run(loop_agent, _mock_response(content=protocol_only, finish_reason="stop"))

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert "only internal output" in result["final_response"]
        assert "`terminal`" in result["final_response"]
        assert "Result: a.txt" in result["final_response"]
        assert "tool_call" not in result["final_response"]

    def test_protocol_only_final_text_without_activity_ends_honestly(self, loop_agent):
        """No tool call recorded: the notice is just the honest head sentence."""
        from tests.agent.test_run_agent import _mock_response

        protocol_only = "<|tool_call:start|>terminal<|tool_call:end|>"
        result = _run(loop_agent, _mock_response(content=protocol_only, finish_reason="stop"))

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert "only internal output" in result["final_response"]
        assert "tool_call" not in result["final_response"]

    def test_dropped_tool_call_ends_the_turn_with_a_notice(self, loop_agent):
        """finish_reason=tool_calls with an empty tool_calls array ends the
        turn in one call: the narration stays, plus a notice that nothing ran."""
        result = _run(loop_agent, _dropped_tool_call_response("Let me verify the PR and gather evidence."))

        assert loop_agent.client.chat.completions.create.call_count == 1, (
            "A dropped tool call must end the turn with a notice, not re-prompt."
        )
        assert "Let me verify the PR" in result["final_response"]
        assert "signaled a tool call but sent none" in result["final_response"]

    def test_dropped_tool_call_notice_includes_last_activity(self, loop_agent):
        loop_agent._last_tool_activity = {
            "tool": "read_file", "args": '{"path": "x.py"}', "result": "print(1)",
            "error": False, "blocked": False, "duration_s": 0.2, "ts": 1000.0,
        }
        result = _run(loop_agent, _dropped_tool_call_response("Let me check the file."))

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert "`read_file`" in result["final_response"]
        assert "Result: print(1)" in result["final_response"]

    def test_clean_stop_text_turn_is_unaffected(self, loop_agent):
        """A genuine finish_reason=stop text response must exit normally — the
        notice path must not fire on ordinary final answers."""
        from tests.agent.test_run_agent import _mock_response

        result = _run(loop_agent, _mock_response(content="Here is your answer.", finish_reason="stop"))

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert "Here is your answer." in result["final_response"]

    def test_persistent_dropped_tool_calls_end_without_looping(self, loop_agent):
        """If the model never emits a call, the turn ends on the first stall
        instead of burning re-prompt calls; the loop never spins."""
        from tests.agent.test_run_agent import _mock_response

        responses = [_dropped_tool_call_response("Let me check.") for _ in range(9)]
        responses.append(_mock_response(content="done", finish_reason="stop"))
        result = _run(loop_agent, *responses)

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert result is not None
        assert "signaled a tool call but sent none" in result["final_response"]


def test_dropped_toolcall_nudge_stays_ephemeral_for_old_transcripts():
    """The re-prompt pair is no longer created, but older transcripts can
    still carry ``_dropped_toolcall_nudge`` rows: the persistence filter must
    keep classifying them as ephemeral scaffolding so they are never written
    to the durable store."""
    from agent.session_persistence import _is_ephemeral_scaffolding

    assert _is_ephemeral_scaffolding(
        {"role": "user", "content": "nudge", "_dropped_toolcall_nudge": True}
    )
