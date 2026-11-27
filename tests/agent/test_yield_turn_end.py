"""Tests for the F3 yield turn end in the real agent loop.

A model turn that calls sessions_yield with pending background children
ends after that one tool round: exactly one provider call, the
acknowledgment as the final response, and a completed (not interrupted,
not failed) turn — so the session goes idle and the children's completion
can re-enter as a new turn. Zero LLM calls: the provider is mocked.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent
from tools.subagent_run_registry import register_run
from tools.turn_control_tools import SESSIONS_YIELD_SCHEMA


def _tool_def(schema):
    return {"type": "function", "function": schema}


def _mock_response(content="", finish_reason="tool_calls", tool_calls=None, usage=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None, reasoning=None)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    resp = SimpleNamespace(choices=[choice], model="test/model")
    resp.usage = SimpleNamespace(**usage) if usage else None
    return resp


def _yield_call(arguments):
    return SimpleNamespace(
        id="call-yield-1", type="function",
        function=SimpleNamespace(name="sessions_yield", arguments=arguments),
    )


def _new_test_agent():
    with (
        patch("model_tools.get_tool_definitions", return_value=[_tool_def(SESSIONS_YIELD_SCHEMA)]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key", provider="openrouter", model="test/model",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
        )
        agent.client = MagicMock()
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.compression_enabled = True
        agent.save_trajectories = False
        return agent


class TestYieldEndsTurn:
    def test_yield_ends_turn_with_acknowledgment(self):
        agent = _new_test_agent()
        register_run("sa-loop-1", goal="research A", owner_agent_session_id=agent.session_id)
        register_run("sa-loop-2", goal="research B", owner_agent_session_id=agent.session_id)
        agent.client.chat.completions.create.return_value = _mock_response(
            tool_calls=[_yield_call(json.dumps({"acknowledgment": "Waiting for the two researchers."}))])
        result = agent.run_conversation("research both topics in the background")
        assert agent.client.chat.completions.create.call_count == 1  # no follow-up model call
        assert result["final_response"] == "Waiting for the two researchers."
        assert result["completed"] is True
        assert result.get("failed") is False
        assert result.get("interrupted") is False
        assert result["api_calls"] == 1

    def test_yield_with_nothing_pending_does_not_end_turn(self):
        agent = _new_test_agent()
        agent.client.chat.completions.create.side_effect = [
            _mock_response(tool_calls=[_yield_call("{}")]),
            _mock_response(content="No children running, so I did the work myself.", finish_reason="stop"),
        ]
        result = agent.run_conversation("do the work")
        assert agent.client.chat.completions.create.call_count == 2
        assert result["final_response"] == "No children running, so I did the work myself."
        assert result["completed"] is True
