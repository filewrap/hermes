"""Gateway /last shows the last thing the agent did in the session.

Drives the real GatewayRunner._handle_last_command with a fake session
store and fake resident/cached agents, mirroring the /agents command tests.
"""

from types import SimpleNamespace

import pytest


def _make_runner(agent=None, cached=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._background_tasks = set()
    runner._cached_agent_for = lambda key: cached

    class _Store:
        _store = None

        async def get_or_create_session(self, source):
            return SimpleNamespace(session_key="agent:main:test:dm:1")

    runner.session_store = None
    runner._async_session_store = _Store()
    if agent is not None:
        runner._running_agents["agent:main:test:dm:1"] = agent
    return runner


class _Event:
    source = SimpleNamespace(platform="telegram", chat_id="1", user_id="100")


def _agent(summary):
    return SimpleNamespace(get_activity_summary=lambda: summary)


def _summary(**overrides):
    base = {
        "current_tool": None,
        "last_tool_activity": {
            "tool": "terminal",
            "args": '{"command": "ls"}',
            "result": "a.txt",
            "error": False,
            "blocked": False,
            "duration_s": 0.8,
            "ts": 1000.0,
        },
        "last_activity_desc": "tool completed: terminal (0.8s)",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_last_shows_tool_call_and_response():
    runner = _make_runner(agent=_agent(_summary()))
    out = await runner._handle_last_command(_Event())
    assert "Last activity" in out
    assert "`terminal`" in out
    assert "Arguments: command: ls" in out
    assert "Result: a.txt" in out
    assert '"command": "ls"' not in out


@pytest.mark.asyncio
async def test_last_shows_running_tool_first():
    runner = _make_runner(agent=_agent(_summary(current_tool="read_file")))
    out = await runner._handle_last_command(_Event())
    assert out.startswith("Currently running: `read_file`")
    assert "`terminal`" in out


@pytest.mark.asyncio
async def test_last_with_no_activity_says_so():
    runner = _make_runner(agent=_agent({"current_tool": None, "last_tool_activity": None,
                                        "last_activity_desc": ""}))
    out = await runner._handle_last_command(_Event())
    assert "No activity recorded yet" in out


@pytest.mark.asyncio
async def test_last_without_any_agent_says_so():
    runner = _make_runner()
    out = await runner._handle_last_command(_Event())
    assert "No activity recorded yet" in out


@pytest.mark.asyncio
async def test_last_falls_back_to_cached_agent():
    runner = _make_runner(cached=_agent(_summary()))
    out = await runner._handle_last_command(_Event())
    assert "`terminal`" in out
    assert "Result: a.txt" in out
