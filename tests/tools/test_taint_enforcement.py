"""Wave-5 taint enforcement: the three provisional refusals at their handlers.

PROVISIONAL (approved provisionally 2026-11-11): a tainted turn cannot spend
credentials/secrets, cannot approve things, cannot send messages to other
chats. These tests prove each refusal fires at the real dispatch path — the
registry for ``telegram_client``/``browser_vault`` (``parent_agent`` threaded
through ``handle_function_call``), the inline executors for
``conversations_turn``/``sessions_send``/``suggest_task``/``message_agent`` —
and that the untainted path still reaches the effect. Zero LLM calls; no live
gateway, no live Telegram connection, no real vault backend.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.turn_taint import get_turn_taint_state
from tools.registry import registry


def _agent(**extra):
    agent = SimpleNamespace(session_id="s-taint", _parent_session_id="", platform="cli")
    for key, value in extra.items():
        setattr(agent, key, value)
    return agent


def _tainted_agent(**extra):
    agent = _agent(**extra)
    get_turn_taint_state(agent).observe("network")
    return agent


# --- parent_agent threading ---------------------------------------------------
# The turn loop threads the live agent through handle_function_call (both the
# concurrent invoke_tool path and the sequential executor path) so registry
# handlers can gate on per-turn state.


@pytest.fixture
def probe_tool():
    seen = {}
    registry.register(
        name="taint_probe_dispatch", toolset="test",
        schema={"name": "taint_probe_dispatch", "description": "probe"},
        handler=lambda args, **kw: seen.update(kw) or "{}")
    try:
        yield seen
    finally:
        registry.deregister("taint_probe_dispatch")


def test_handle_function_call_forwards_parent_agent(probe_tool):
    from model_tools import handle_function_call

    agent = _agent()
    handle_function_call("taint_probe_dispatch", {}, parent_agent=agent)
    assert probe_tool.get("parent_agent") is agent


def test_handle_function_call_without_agent_passes_none(probe_tool):
    from model_tools import handle_function_call

    handle_function_call("taint_probe_dispatch", {})
    assert "parent_agent" not in probe_tool


def test_invoke_tool_forwards_parent_agent(probe_tool):
    from agent.agent_runtime_helpers import invoke_tool

    agent = _agent(session_id="s-taint", valid_tool_names=[],
                   _current_turn_id="", _current_api_request_id="",
                   enabled_toolsets=None, disabled_toolsets=None,
                   _tenant_scope=None, _memory_manager=None)
    invoke_tool(agent, "taint_probe_dispatch", {}, "task",
                pre_tool_block_checked=True, skip_tool_request_middleware=True,
                skip_tool_execution_middleware=True)
    assert probe_tool.get("parent_agent") is agent


def test_sequential_dispatch_forwards_parent_agent(probe_tool):
    from agent.tool_executor import _ToolCallRef, _resolve_sequential_dispatch

    agent = _agent(session_id="s-taint", valid_tool_names=[],
                   _current_turn_id="", _current_api_request_id="",
                   enabled_toolsets=None, disabled_toolsets=None,
                   _context_engine_tool_names=set(), _memory_manager=None,
                   quiet_mode=False)
    ref = _ToolCallRef("taint_probe_dispatch", {}, "task", "call-1", [])
    dispatch = _resolve_sequential_dispatch(agent, ref, [])
    dispatch.execute({})
    assert probe_tool.get("parent_agent") is agent


# --- telegram_client ----------------------------------------------------------


def _telegram_adapter(home):
    from gateway.config import Platform
    from gateway.session import SessionSource

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-10042", chat_type="group",
                           user_id="100", thread_id="77")
    adapter = SimpleNamespace(
        _client=object(), build_source=lambda *_a, **_kw: source,
        _settings_home=lambda _s: home, _authorized=lambda _s: True,
        _is_owner_user=lambda _u, **_kw: True,
        request_telegram_operation=AsyncMock(return_value={"pending_confirmation": True}),
    )
    return source, adapter


def _telegram_call(monkeypatch, tmp_path, args, parent_agent=None):
    import tools.send_message_senders as senders
    from gateway.session_context import clear_session_vars, set_session_vars
    from model_tools import handle_function_call

    source, adapter = _telegram_adapter(tmp_path)
    runner = SimpleNamespace(_gateway_loop=None, _get_cached_session_source=lambda _k: source)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(senders, "_live_adapter", lambda _platform: (runner, adapter))
    fields = dict(platform="telegram", chat_id="-10042", chat_type="group", user_id="100",
                  thread_id="77", session_key="test-session")
    tokens = set_session_vars(**fields)
    try:
        kwargs = {} if parent_agent is None else {"parent_agent": parent_agent}
        result = json.loads(handle_function_call("telegram_client", args, **kwargs))
    finally:
        clear_session_vars(tokens)
    return result, adapter


def test_tainted_send_to_other_chat_refused_before_transport(monkeypatch, tmp_path):
    result, adapter = _telegram_call(
        monkeypatch, tmp_path,
        {"action": "send_message", "params": {"recipient": "-10099", "content": "hi"}},
        parent_agent=_tainted_agent())
    assert "tainted" in result["error"]
    adapter.request_telegram_operation.assert_not_awaited()


def test_tainted_send_to_current_chat_still_reaches_transport(monkeypatch, tmp_path):
    result, adapter = _telegram_call(
        monkeypatch, tmp_path,
        {"action": "send_message", "params": {"recipient": "-10042", "content": "hi"}},
        parent_agent=_tainted_agent())
    assert result == {"pending_confirmation": True}
    adapter.request_telegram_operation.assert_awaited_once()


def test_tainted_cross_chat_reply_refused_before_transport(monkeypatch, tmp_path):
    result, adapter = _telegram_call(
        monkeypatch, tmp_path,
        {"action": "cross_chat_reply", "params": {"message_id": 7, "content": "hi"}},
        parent_agent=_tainted_agent())
    assert "tainted" in result["error"]
    adapter.request_telegram_operation.assert_not_awaited()


def test_untainted_send_to_other_chat_reaches_transport(monkeypatch, tmp_path):
    result, adapter = _telegram_call(
        monkeypatch, tmp_path,
        {"action": "send_message", "params": {"recipient": "-10099", "content": "hi"}},
        parent_agent=_agent())
    assert result == {"pending_confirmation": True}
    adapter.request_telegram_operation.assert_awaited_once()


def test_out_of_turn_send_is_not_taint_gated(monkeypatch, tmp_path):
    # No agent, no turn, no taint: an out-of-turn caller (MCP server, code
    # kernel) is fail-open, exactly as before this change.
    result, adapter = _telegram_call(
        monkeypatch, tmp_path,
        {"action": "send_message", "params": {"recipient": "-10099", "content": "hi"}})
    assert result == {"pending_confirmation": True}
    adapter.request_telegram_operation.assert_awaited_once()


# --- browser_vault ------------------------------------------------------------


def test_tainted_vault_fill_refused_without_touching_the_page(monkeypatch):
    import tools.browser_vault_tool as vault_mod

    calls = []
    monkeypatch.setattr(vault_mod, "_fenced_page_op",
                        lambda tid, fn: calls.append(fn) or '{"success": true}')
    result = json.loads(registry.dispatch(
        "browser_vault", {"action": "fill", "handle": "h1"},
        parent_agent=_tainted_agent()))
    assert result["success"] is False
    assert "tainted" in result["error"]
    assert calls == []


def test_tainted_vault_enter_code_refused_without_touching_the_page(monkeypatch):
    import tools.browser_vault_tool as vault_mod

    calls = []
    monkeypatch.setattr(vault_mod, "_fenced_page_op",
                        lambda tid, fn: calls.append(fn) or '{"success": true}')
    result = json.loads(registry.dispatch(
        "browser_vault", {"action": "enter_code", "handle": "h1"},
        parent_agent=_tainted_agent()))
    assert result["success"] is False
    assert "tainted" in result["error"]
    assert calls == []


def test_untainted_vault_fill_reaches_the_page(monkeypatch):
    import tools.browser_vault_tool as vault_mod

    calls = []
    monkeypatch.setattr(vault_mod, "_fenced_page_op",
                        lambda tid, fn: calls.append(fn) or '{"success": true}')
    result = json.loads(registry.dispatch(
        "browser_vault", {"action": "fill", "handle": "h1"},
        parent_agent=_agent()))
    assert result == {"success": True}
    assert len(calls) == 1


# --- conversations_turn -------------------------------------------------------


def _conversation_ref(tmp_path, monkeypatch):
    import tools.conversation_registry as conv_registry
    import tools.conversation_tools as tools_mod

    monkeypatch.setattr(conv_registry, "STORE_PATH", tmp_path / "conversations.json")
    monkeypatch.setattr(tools_mod, "_telegram_context", lambda: ("-10042", "100", None))
    return conv_registry.record_conversation(
        "telegram", "-10042", type="group", name="Testing Agents")


def test_tainted_conversations_turn_refused_without_sending(monkeypatch, tmp_path):
    import tools.conversation_tools as tools_mod
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext

    ref = _conversation_ref(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(tools_mod, "_send_to_chat",
                        lambda chat_id, message: calls.append((chat_id, message)) or (123, None))
    ctx = InlineToolContext(effective_task_id="t-1", tool_call_id="call-1")
    result = json.loads(INLINE_TOOL_EXECUTORS["conversations_turn"](
        _tainted_agent(), {"conversation_ref": ref, "message": "hi", "timeoutSeconds": 5}, ctx))
    assert result["success"] is False
    assert "tainted" in result["error"]
    assert calls == []


# --- sessions_send ------------------------------------------------------------


def test_tainted_sessions_send_refused_without_delivering(monkeypatch):
    import tools.sessions_send_tool as send_mod

    calls = []
    monkeypatch.setattr(send_mod, "_send_to_child",
                        lambda key, message, agent: calls.append(key) or {"ok": True})
    result = json.loads(send_mod.sessions_send(
        session_key="run-1", message="hi", parent_agent=_tainted_agent()))
    assert result["success"] is False
    assert "tainted" in result["error"]
    assert calls == []


def test_untainted_sessions_send_reaches_the_child(monkeypatch):
    import tools.sessions_send_tool as send_mod

    calls = []
    monkeypatch.setattr(send_mod, "_send_to_child",
                        lambda key, message, agent: calls.append(key) or {"ok": True})
    result = json.loads(send_mod.sessions_send(
        session_key="run-1", message="hi", parent_agent=_agent()))
    assert result["success"] is True
    assert calls == ["run-1"]


# --- message_agent (Bot Mode teammate DM) -------------------------------------


def _managed_home(tmp_path, *, teammates=("researcher",)):
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    for name in teammates:
        d = home / "profiles" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "profile.yaml").write_text(
            textwrap.dedent(
                """\
                description: teammate for tests
                ui_meta:
                  hermes-bots:
                    shape: cloud
                """
            ),
            encoding="utf-8",
        )
    return home


class _FakeDB:
    def __init__(self, home, title):
        self.db_path = str(home / "state.db")
        self._title = title

    def get_session_title(self, _sid):
        return self._title


class _FakeAgent:
    def __init__(self, home, title="Bot Chat"):
        self._session_db = _FakeDB(home, title)
        self.session_id = "sess-1"
        self._session_title_hint = None
        self._bot_mode_protocol = True
        self.tools = []
        self.valid_tool_names = set()


def test_tainted_message_agent_refused_without_sending(monkeypatch, tmp_path):
    import tools.bot_mode_dm as dm_mod
    import tools.bot_mode_dm_chain as chain_mod
    from tools import bot_mode_probe

    bot_mode_probe._reset_cache_for_tests()
    home = _managed_home(tmp_path)
    agent = _FakeAgent(home)
    get_turn_taint_state(agent).observe("network")
    calls = []
    monkeypatch.setattr(chain_mod, "send_gated",
                        lambda *a, **k: calls.append(a) or '{"success": true}')
    result = json.loads(dm_mod.message_agent_tool(
        target="researcher", message="hi", agent=agent))
    assert "error" in result
    assert "tainted" in result["error"]
    assert calls == []
    bot_mode_probe._reset_cache_for_tests()


def test_untainted_message_agent_reaches_the_chain(monkeypatch, tmp_path):
    import tools.bot_mode_dm as dm_mod
    import tools.bot_mode_dm_chain as chain_mod
    from tools import bot_mode_probe

    bot_mode_probe._reset_cache_for_tests()
    home = _managed_home(tmp_path)
    agent = _FakeAgent(home)
    calls = []
    monkeypatch.setattr(chain_mod, "send_gated",
                        lambda *a, **k: calls.append(a) or '{"success": true}')
    result = json.loads(dm_mod.message_agent_tool(
        target="researcher", message="hi", agent=agent))
    assert result == {"success": True}
    assert len(calls) == 1
    bot_mode_probe._reset_cache_for_tests()


# --- suggest_task autonomous auto-approve -------------------------------------


def test_tainted_suggest_task_never_auto_approves(monkeypatch):
    import tools.suggest_task_tool as tools_mod

    monkeypatch.setattr(tools_mod, "_autonomous_mode_enabled", lambda: True)
    calls = []

    def _spawn(args):
        calls.append(args)
        return json.dumps({"success": True, "results": [
            {"status": "completed", "subagent_id": "run-1", "summary": "done"}]})

    agent = _tainted_agent()
    agent._dispatch_delegate_task = _spawn
    result = json.loads(tools_mod.suggest_task(
        title="Tidy the downloads folder", prompt="Organize ~/Downloads by kind.",
        parent_agent=agent))
    assert result["status"] == "pending"
    assert "tainted" in result["message"]
    assert calls == []


def test_untainted_suggest_task_auto_approves_when_autonomous(monkeypatch):
    import tools.suggest_task_tool as tools_mod

    monkeypatch.setattr(tools_mod, "_autonomous_mode_enabled", lambda: True)
    calls = []

    def _spawn(args):
        calls.append(args)
        return json.dumps({"success": True, "results": [
            {"status": "completed", "subagent_id": "run-1", "summary": "done"}]})

    agent = _agent()
    agent._dispatch_delegate_task = _spawn
    result = json.loads(tools_mod.suggest_task(
        title="Tidy the downloads folder", prompt="Organize ~/Downloads by kind.",
        parent_agent=agent))
    assert result["status"] == "started"
    assert len(calls) == 1


# --- delegated-child auto-approve callback ------------------------------------


def test_tainted_parent_denies_child_auto_approve(monkeypatch):
    import tools.delegate_tool_config as cfg_mod

    monkeypatch.setattr(cfg_mod, "_cfg", lambda: {"subagent_auto_approve": True})
    assert cfg_mod._get_subagent_approval_callback(_tainted_agent()) is cfg_mod._subagent_auto_deny
    assert cfg_mod._get_subagent_approval_callback(_tainted_agent())("rm -rf /", "wipe") == "deny"


def test_untainted_parent_keeps_configured_auto_approve(monkeypatch):
    import tools.delegate_tool_config as cfg_mod

    monkeypatch.setattr(cfg_mod, "_cfg", lambda: {"subagent_auto_approve": True})
    assert cfg_mod._get_subagent_approval_callback(_agent()) is cfg_mod._subagent_auto_approve
    assert cfg_mod._get_subagent_approval_callback(_agent())("rm -rf /", "wipe") == "once"
    monkeypatch.setattr(cfg_mod, "_cfg", dict)
    assert cfg_mod._get_subagent_approval_callback(_agent()) is cfg_mod._subagent_auto_deny
