"""Guest history control: the save-history setting and the ephemeral guest turn.

Invariant tests, written red-first. A guest turn with saving OFF must leave
nothing behind: no transcript rows (the agent's persistence is disabled, so
the gateway's transcript write skips too) and no cached agent carrying the
turn's messages in memory (else the next turn inherits them through the
cached-history lag guard). A guest turn with saving ON, and every non-guest
turn, behaves exactly as before.
"""
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.guest_history import (
    guest_history_saving_enabled,
    is_ephemeral_guest_turn,
    is_guest_thread_id,
)


def _source(thread_id):
    return SimpleNamespace(
        platform=Platform.TELEGRAM, chat_id="c1", user_id="u1",
        chat_type="dm", thread_id=thread_id,
    )


def _config(save_history):
    if save_history is None:
        return {}
    return {"platforms": {"telegram": {"guest_save_history": save_history}}}


# --- predicates ---------------------------------------------------------------

def test_guest_thread_is_the_guest_prefix_only():
    assert is_guest_thread_id("guest-77")
    assert is_guest_thread_id("guest-")
    assert not is_guest_thread_id("1234")
    assert not is_guest_thread_id("")
    assert not is_guest_thread_id(None)


def test_guest_history_saving_defaults_to_on():
    assert guest_history_saving_enabled({}) is True
    assert guest_history_saving_enabled({"platforms": {}}) is True
    assert guest_history_saving_enabled({"platforms": {"telegram": {}}}) is True
    assert guest_history_saving_enabled({"platforms": {"telegram": {"guest_save_history": True}}}) is True


def test_guest_history_saving_off_when_configured():
    assert guest_history_saving_enabled(_config(False)) is False


def test_ephemeral_only_for_a_guest_thread_with_saving_off():
    assert is_ephemeral_guest_turn(_source("guest-77"), _config(False)) is True
    assert is_ephemeral_guest_turn(_source("guest-77"), _config(True)) is False
    assert is_ephemeral_guest_turn(_source("guest-77"), _config(None)) is False
    assert is_ephemeral_guest_turn(_source("1234"), _config(False)) is False
    assert is_ephemeral_guest_turn(_source(None), _config(False)) is False


# --- the TurnRunner flags the agent ------------------------------------------

def _turn_runner(thread_id, save_history):
    from gateway.run_turn_runner import TurnRunner

    ctx = SimpleNamespace(
        source=_source(thread_id), user_config=_config(save_history),
        session_id="sess", message="", deep_research=None,
    )
    return TurnRunner(SimpleNamespace(session_authority=None), ctx)


def test_ephemeral_guest_turn_disables_agent_persistence():
    agent = SimpleNamespace(_persist_disabled=False)
    _turn_runner("guest-77", False)._apply_ephemeral_guest_flag(agent)
    assert agent._persist_disabled is True


def test_guest_turn_with_saving_on_keeps_persistence():
    agent = SimpleNamespace(_persist_disabled=False)
    _turn_runner("guest-77", True)._apply_ephemeral_guest_flag(agent)
    assert agent._persist_disabled is False


def test_non_guest_turn_is_never_flagged_even_with_saving_off():
    agent = SimpleNamespace(_persist_disabled=False)
    _turn_runner("1234", False)._apply_ephemeral_guest_flag(agent)
    assert agent._persist_disabled is False


def test_flag_is_reset_when_saving_turns_back_on():
    # A reused cached agent built under saving-off must serve a saving-on
    # turn with persistence restored, not stay disabled forever.
    runner = _turn_runner("guest-77", True)
    agent = SimpleNamespace(_persist_disabled=True)
    runner._apply_ephemeral_guest_flag(agent)
    assert agent._persist_disabled is False


# --- the eviction after an ephemeral turn ------------------------------------

def _cache_runner(agent, *, persist_disabled):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._agent_cache_lock = None
    agent._persist_disabled = persist_disabled
    runner._agent_cache = {"agent:main:telegram:dm:c1:guest-77": (agent, "sig", 0, "sess")}
    runner._running_agent_items = lambda: []
    released = []
    runner._spawn_release_thread = lambda target, args, name, **kw: released.append((target, args))
    runner._release_evicted_agent_soft = lambda a: released.append(("soft", a))
    return runner, released


def test_ephemeral_guest_agent_is_evicted_after_the_turn():
    agent = SimpleNamespace()
    runner, released = _cache_runner(agent, persist_disabled=True)
    assert runner._evict_ephemeral_guest_agent(
        "agent:main:telegram:dm:c1:guest-77", _source("guest-77")) is True
    assert "agent:main:telegram:dm:c1:guest-77" not in runner._agent_cache
    assert released  # the evicted agent's clients are released, not leaked


def test_guest_agent_with_saving_on_is_kept():
    agent = SimpleNamespace()
    runner, _released = _cache_runner(agent, persist_disabled=False)
    assert runner._evict_ephemeral_guest_agent(
        "agent:main:telegram:dm:c1:guest-77", _source("guest-77")) is False
    assert "agent:main:telegram:dm:c1:guest-77" in runner._agent_cache


def test_non_guest_flagged_agent_is_kept():
    # _persist_disabled is also the review-fork flag; a non-guest session's
    # agent is never this helper's business.
    agent = SimpleNamespace()
    runner, _released = _cache_runner(agent, persist_disabled=True)
    assert runner._evict_ephemeral_guest_agent(
        "agent:main:telegram:dm:c1", _source("1234")) is False
    assert "agent:main:telegram:dm:c1:guest-77" in runner._agent_cache


def test_evict_is_a_noop_without_a_cached_agent():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._agent_cache_lock = None
    runner._agent_cache = {}
    runner._running_agent_items = lambda: []
    assert runner._evict_ephemeral_guest_agent(
        "agent:main:telegram:dm:c1:guest-77", _source("guest-77")) is False
