"""Per-turn taint state and its memory-write provenance consumer.

Behaviour contracts for ``agent/turn_taint.py`` (ported shape): the sticky
per-turn boolean, ``presentation_only`` not tainting, and the origin class a
memory write carries depending on whether the turn observed network content.
"""
from __future__ import annotations

from types import SimpleNamespace

from agent.background_review import build_memory_write_metadata
from agent.turn_taint import (
    TurnTaintState,
    get_turn_taint_state,
    is_turn_tainted,
    observe_tool_outcome,
    refuse_tainted_auto_approve,
    refuse_tainted_cross_chat_send,
    refuse_tainted_telegram_send,
    reset_turn_taint,
    turn_taint_origin_class,
)
from tools.registry import registry


def _agent():
    return SimpleNamespace(session_id="s1", _parent_session_id="", platform="cli")


def _tainted_agent():
    agent = _agent()
    get_turn_taint_state(agent).observe("network")
    assert is_turn_tainted(agent)
    return agent


def test_state_machine_is_sticky_and_resettable():
    state = TurnTaintState()
    assert not state.is_tainted()
    state.observe("local")
    assert not state.is_tainted()
    state.observe("network", presentation_only=True)  # rendered only, never fed back
    assert not state.is_tainted()
    state.observe("network")
    assert state.is_tainted()
    state.observe("local")  # sticky: a later local result does not clear it
    assert state.is_tainted()
    state.reset()
    assert not state.is_tainted()


def test_observe_tool_outcome_reads_registry_entry():
    agent = _agent()
    registry.register(
        name="taint_probe_network", toolset="test", schema={"name": "x", "description": "x"},
        handler=lambda args, **kw: "{}", result_content_source="network")
    registry.register(
        name="taint_probe_local", toolset="test", schema={"name": "x", "description": "x"},
        handler=lambda args, **kw: "{}", result_content_source=None)
    try:
        assert not observe_tool_outcome(agent, "taint_probe_local")
        assert not is_turn_tainted(agent)
        assert observe_tool_outcome(agent, "taint_probe_network")
        assert is_turn_tainted(agent)
        reset_turn_taint(agent)
        assert not is_turn_tainted(agent)
        assert not observe_tool_outcome(agent, "no_such_tool")  # unknown tools do not taint
    finally:
        registry.deregister("taint_probe_network")
        registry.deregister("taint_probe_local")


def test_origin_class_in_memory_write_metadata():
    agent = _agent()
    assert build_memory_write_metadata(agent)["origin_class"] == "agent"
    registry.register(
        name="taint_probe_network", toolset="test", schema={"name": "x", "description": "x"},
        handler=lambda args, **kw: "{}", result_content_source="network")
    try:
        observe_tool_outcome(agent, "taint_probe_network")
        assert turn_taint_origin_class(agent) == "untrusted"
        assert build_memory_write_metadata(agent)["origin_class"] == "untrusted"
    finally:
        registry.deregister("taint_probe_network")
    reset_turn_taint(agent)
    assert build_memory_write_metadata(agent)["origin_class"] == "agent"


# --- Provisional refusals (wave 5) -------------------------------------------
# PROVISIONAL (approved provisionally 2026-11-11): a tainted turn cannot spend
# credentials, cannot approve things, cannot send messages to other chats.


def test_cross_chat_send_refusal_only_when_tainted():
    assert refuse_tainted_cross_chat_send(None) is None  # no turn, no taint
    assert refuse_tainted_cross_chat_send(_agent()) is None
    refusal = refuse_tainted_cross_chat_send(_tainted_agent())
    assert refusal is not None and "tainted" in refusal


def test_telegram_send_refusal_is_recipient_scoped():
    agent = _agent()
    tainted = _tainted_agent()
    # Untainted: everything passes the gate.
    assert refuse_tainted_telegram_send(agent, action="send_message",
                                        params={"recipient": "-10099"},
                                        current_chat_id="-10042") is None
    # Tainted, same-chat numeric send: allowed (answering the user stays open).
    assert refuse_tainted_telegram_send(tainted, action="send_message",
                                        params={"recipient": "-10042"},
                                        current_chat_id="-10042") is None
    # Tainted, another numeric chat: refused.
    refusal = refuse_tainted_telegram_send(tainted, action="send_message",
                                           params={"recipient": "-10099"},
                                           current_chat_id="-10042")
    assert refusal is not None and "tainted" in refusal
    # Tainted, @username: never provably this chat, refused fail-closed.
    refusal = refuse_tainted_telegram_send(tainted, action="send_message",
                                           params={"recipient": "@someone"},
                                           current_chat_id="-10042")
    assert refusal is not None and "tainted" in refusal
    # Tainted, cross-chat reply: always refused (quote_reply covers same-chat).
    refusal = refuse_tainted_telegram_send(tainted, action="cross_chat_reply",
                                           params={"message_id": 7},
                                           current_chat_id="-10042")
    assert refusal is not None and "tainted" in refusal
    # Tainted, other actions (reads, same-chat sends, reactions): untouched.
    assert refuse_tainted_telegram_send(tainted, action="rich_message_send",
                                        params={"content": "hi"},
                                        current_chat_id="-10042") is None
    assert refuse_tainted_telegram_send(tainted, action="quote_reply",
                                        params={"message_id": 7},
                                        current_chat_id="-10042") is None
    assert refuse_tainted_telegram_send(tainted, action="capabilities",
                                        current_chat_id="-10042") is None


def test_auto_approve_refusal_only_when_tainted():
    assert refuse_tainted_auto_approve(None) is None
    assert refuse_tainted_auto_approve(_agent()) is None
    assert "tainted" in (refuse_tainted_auto_approve(_tainted_agent()) or "")
