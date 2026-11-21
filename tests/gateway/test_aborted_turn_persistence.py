"""Aborted-turn persistence: the approved resume matrix (2026-11-11).

(a) No per-process caching — turn state is already flushed incrementally to
SessionDB as it runs (``_persist_session`` per step, one SQLite transaction per
flush); only resuming from it is new. Covered by the existing flush /
transcript tests; nothing new is cached here.

(b) Forced stop (/stop): the turn is marked interrupted and NEVER auto-resumed
— a durable stop fence bound to the live turn's token, recorded before the
interrupt lands, so a crash or shutdown mark in the unwind window cannot
re-arm it, and an already-set ``resume_pending`` flag is cleared outright.

(c) Model change mid-turn: the turn ends, its partial state stays as history,
and the next turn starts fresh with the new model — NO resume under the new
model. The interrupted turn's (provider, model) rides its marker, and both
resume paths (boot schedule, next user message) refuse a known mismatch.

(d) Crash / gateway restart: resume automatically from the last persisted
point — the existing crash-left marker promotion + boot drain, extended here
only by carrying the model stamp through it.
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionEntry, SessionSource, SessionStore
from gateway.session_lifecycle import (
    resume_model_matches,
    sanitize_turn_model,
    turn_model_from_agent,
    turn_model_key,
)
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)


ACTIVE_TURN_MAX_AGE_SECONDS = 60 * 60
OLD_MODEL = {"model": "old-model", "provider": "openrouter"}
NEW_MODEL = {"model": "new-model", "provider": "openrouter"}


def _make_source(chat_id: str = "aborted-chat") -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        user_id="user-1",
        chat_type="channel",
        thread_id="thread-1",
    )


def _make_store(tmp_path) -> SessionStore:
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    store._db = None
    return store


def _entry_for(store: SessionStore, source: SessionSource) -> SessionEntry:
    key = store._generate_session_key(source)
    with store._lock:
        store._ensure_loaded_locked()
        return store._entries[key]


# ---------------------------------------------------------------------------
# (b) Forced stop never resumes
# ---------------------------------------------------------------------------


def test_stop_fence_blocks_crash_promotion(tmp_path):
    """(b) /stop, then a crash before the turn unwinds: the fenced marker is
    cleared without resuming — the stop wins over the crash."""
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    token = store.mark_turn_active(entry.session_key)

    assert store.mark_turn_force_stopped(entry.session_key) is True
    assert _entry_for(store, source).stop_fence_token == token

    assert store.recover_interrupted_turns() == 0
    recovered = _entry_for(store, source)
    assert recovered.resume_pending is False
    assert recovered.resume_reason is None
    assert recovered.active_turn_token is None
    assert recovered.stop_fence_token is None


def test_stop_clears_an_already_set_resume_pending(tmp_path):
    """(b) A session already flagged for resume whose turn is then force-stopped
    does not re-arm on the next boot: the flag (and its model) is cleared."""
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    assert store.mark_resume_pending(entry.session_key, "shutdown_timeout", model=OLD_MODEL) is True
    assert _entry_for(store, source).resume_pending is True

    assert store.mark_turn_force_stopped(entry.session_key) is True
    cleared = _entry_for(store, source)
    assert cleared.resume_pending is False
    assert cleared.resume_reason is None
    assert cleared.resume_model is None
    assert cleared.stop_fence_token is None  # no live turn to fence


def test_mark_resume_pending_refuses_a_fenced_session(tmp_path):
    """(b) The graceful-shutdown mark cannot re-arm a turn /stop is unwinding."""
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    token = store.mark_turn_active(entry.session_key)
    store.mark_turn_force_stopped(entry.session_key)

    assert store.mark_resume_pending(entry.session_key, "shutdown_timeout") is False
    assert _entry_for(store, source).resume_pending is False

    # Once the fenced turn unwinds cleanly, the fence is spent: a later
    # interruption of a NEW turn may mark again.
    assert store.clear_turn_active(entry.session_key, token) is True
    assert _entry_for(store, source).stop_fence_token is None
    assert store.mark_resume_pending(entry.session_key, "shutdown_timeout") is True


def test_fence_is_spent_on_clean_unwind_and_next_turn_resumes(tmp_path):
    """(b) A /stop followed by a clean unwind leaves nothing behind: the next
    turn's crash still promotes normally."""
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    first = store.mark_turn_active(entry.session_key)
    store.mark_turn_force_stopped(entry.session_key)
    assert store.clear_turn_active(entry.session_key, first) is True

    second = store.mark_turn_active(entry.session_key)
    assert second != first
    assert store.recover_interrupted_turns() == 1
    recovered = _entry_for(store, source)
    assert recovered.resume_pending is True
    assert recovered.resume_reason == "restart_interrupted"


def test_remark_supersedes_the_fence(tmp_path):
    """(b) A newer turn re-marking the slot drops the old fence with the old token."""
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)
    store.mark_turn_force_stopped(entry.session_key)
    assert _entry_for(store, source).stop_fence_token is not None

    store.mark_turn_active(entry.session_key)
    current = _entry_for(store, source)
    assert current.stop_fence_token is None
    assert store.recover_interrupted_turns() == 1


def test_discard_clears_fence_and_model(tmp_path):
    """(b/c) A verified clean shutdown discards the marker, its fence and its stamp."""
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)
    store.stamp_turn_model(entry.session_key, OLD_MODEL)
    store.mark_turn_force_stopped(entry.session_key)

    assert store.discard_active_turn_markers() == 1
    cleared = _entry_for(store, source)
    assert cleared.active_turn_token is None
    assert cleared.active_turn_model is None
    assert cleared.stop_fence_token is None
    assert cleared.resume_pending is False


@pytest.mark.asyncio
async def test_interrupt_and_clear_session_records_the_force_stop():
    """(b) The /stop seam records the fence before interrupting: both production
    callers of ``_interrupt_and_clear_session`` are /stop handlers."""
    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        mark_turn_force_stopped=AsyncMock(return_value=True),
    )
    runner._peek_session_state = MagicMock(return_value=None)
    runner._interrupt_running_turn = MagicMock(return_value=3)
    runner._delivery_adapter_for = MagicMock(return_value=None)
    runner._drop_turn_slot = MagicMock()

    await runner._interrupt_and_clear_session(
        "agent:main:telegram:dm:123", MagicMock(),
        interrupt_reason="Stop requested", invalidation_reason="stop_command",
    )

    runner._async_session_store.mark_turn_force_stopped.assert_awaited_once_with(
        "agent:main:telegram:dm:123"
    )


# ---------------------------------------------------------------------------
# (c) Model change: no resume under the new model
# ---------------------------------------------------------------------------


def test_turn_model_helpers():
    assert sanitize_turn_model({"model": " m ", "provider": " OpenRouter "}) == {
        "model": "m", "provider": "OpenRouter",
    }
    assert sanitize_turn_model({"model": "m", "provider": 42, "api_key": "x"}) == {"model": "m"}
    assert sanitize_turn_model({"provider": "openrouter"}) is None
    assert sanitize_turn_model({"model": "  "}) is None
    assert sanitize_turn_model("openrouter/m") is None
    assert sanitize_turn_model(None) is None

    agent = SimpleNamespace(model="glm-5", provider="zai")
    assert turn_model_from_agent(agent) == {"model": "glm-5", "provider": "zai"}
    assert turn_model_from_agent(SimpleNamespace(model="", provider="zai")) is None
    assert turn_model_from_agent(None) is None

    assert turn_model_key({"model": "m", "provider": "OpenRouter"}) == ("openrouter", "m")
    assert turn_model_key({"model": "m"}) == ("", "m")
    assert turn_model_key(None) is None


def test_resume_model_matches_matrix():
    assert resume_model_matches(OLD_MODEL, {"model": "old-model", "provider": "OpenRouter"}) is True
    assert resume_model_matches(OLD_MODEL, NEW_MODEL) is False
    assert resume_model_matches(OLD_MODEL, {"model": "old-model", "provider": "anthropic"}) is False
    assert resume_model_matches({"model": "old-model"}, {"model": "old-model", "provider": "x"}) is False
    # Unknown on either side fails open: a marker that predates the stamp, or a
    # turn whose model could not be resolved, must not block a resume.
    assert resume_model_matches(None, NEW_MODEL) is True
    assert resume_model_matches(OLD_MODEL, None) is True
    assert resume_model_matches(None, None) is True
    assert resume_model_matches({"model": "  "}, NEW_MODEL) is True


def test_recover_copies_the_turn_model_onto_the_marker(tmp_path):
    """(c/d) A crash-left turn promotes with the model it ran under."""
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)
    assert store.stamp_turn_model(entry.session_key, OLD_MODEL) is True

    assert store.recover_interrupted_turns(
        max_age_seconds=ACTIVE_TURN_MAX_AGE_SECONDS
    ) == 1
    recovered = _entry_for(store, source)
    assert recovered.resume_pending is True
    assert recovered.resume_reason == "restart_interrupted"
    assert recovered.resume_model == OLD_MODEL
    assert recovered.active_turn_model is None  # the marker is spent


def test_recover_without_a_stamp_promotes_with_unknown_model(tmp_path):
    """(c) A marker that predates the stamp still promotes; the gate fails open."""
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)

    assert store.recover_interrupted_turns() == 1
    assert _entry_for(store, source).resume_model is None


def test_stamp_requires_a_live_marker(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)

    assert store.stamp_turn_model(entry.session_key, OLD_MODEL) is False
    assert _entry_for(store, source).active_turn_model is None


def test_mark_resume_pending_records_the_model(tmp_path):
    """(c) The shutdown mark records the interrupted turn's route: explicit model
    wins, otherwise the turn's stamp, and an already-pending marker keeps its own."""
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)
    store.stamp_turn_model(entry.session_key, OLD_MODEL)

    assert store.mark_resume_pending(entry.session_key, "shutdown_timeout") is True
    assert _entry_for(store, source).resume_model == OLD_MODEL

    assert store.mark_resume_pending(
        entry.session_key, "restart_timeout", model=NEW_MODEL) is True
    assert _entry_for(store, source).resume_model == NEW_MODEL


def test_new_fields_round_trip_and_malformed_reads_as_unknown(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)
    store.stamp_turn_model(entry.session_key, OLD_MODEL)
    store.mark_turn_force_stopped(entry.session_key)

    payload = _entry_for(store, source).to_dict()
    assert payload["active_turn_model"] == OLD_MODEL
    assert payload["stop_fence_token"] == payload["active_turn_token"]

    restored = SessionEntry.from_dict(payload)
    assert restored.active_turn_model == OLD_MODEL
    assert restored.stop_fence_token == payload["active_turn_token"]

    payload.pop("active_turn_model")
    payload.pop("resume_model", None)
    payload.pop("stop_fence_token")
    legacy = SessionEntry.from_dict(payload)
    assert legacy.active_turn_model is None
    assert legacy.resume_model is None
    assert legacy.stop_fence_token is None

    payload["active_turn_model"] = {"model": 42}
    payload["resume_model"] = "not-a-dict"
    payload["stop_fence_token"] = {"nope": True}
    corrupt = SessionEntry.from_dict(payload)
    assert corrupt.active_turn_model is None
    assert corrupt.resume_model is None
    assert corrupt.stop_fence_token is None


# ---------------------------------------------------------------------------
# (c) Boot gate: no resume turn under a new model
# ---------------------------------------------------------------------------


def _resume_entry(*, resume_model=None):
    source = make_restart_source(chat_id="model-chat")
    return SessionEntry(
        session_key="agent:main:telegram:dm:model-chat",
        session_id="sid-model",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
        resume_model=resume_model,
    )


@pytest.mark.asyncio
async def test_boot_gate_skips_a_model_changed_resume(monkeypatch):
    """(c) Boot: the interrupted turn ran under old-model, the session now
    resolves to new-model — no resume turn is scheduled and the flag is cleared
    (the transcript stays for the next fresh turn)."""
    runner, adapter = make_restart_runner()
    entry = _resume_entry(resume_model=dict(OLD_MODEL))
    runner.session_store._entries = {entry.session_key: entry}
    cleared: list[str] = []
    runner.session_store.clear_resume_pending = MagicMock(
        side_effect=lambda key: cleared.append(key) or True
    )
    runner._resolve_session_agent_runtime = MagicMock(
        return_value=("new-model", {"provider": "openrouter"})
    )
    monkeypatch.setattr(
        "gateway.session_policy.policy_for_source", lambda *args, **kwargs: None
    )
    adapter.handle_message = AsyncMock()

    assert runner._schedule_resume_pending_sessions() == 0
    await asyncio.sleep(0)

    adapter.handle_message.assert_not_called()
    assert cleared == [entry.session_key]
    assert entry.session_key not in runner._running_agents


@pytest.mark.asyncio
async def test_boot_gate_allows_the_same_model_resume(monkeypatch):
    """(c) Boot: the model is unchanged — the resume turn is scheduled as before."""
    runner, adapter = make_restart_runner()
    entry = _resume_entry(resume_model=dict(OLD_MODEL))
    runner.session_store._entries = {entry.session_key: entry}
    runner._resolve_session_agent_runtime = MagicMock(
        return_value=("old-model", {"provider": "openrouter"})
    )
    monkeypatch.setattr(
        "gateway.session_policy.policy_for_source", lambda *args, **kwargs: None
    )
    adapter.handle_message = AsyncMock()

    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.sleep(0)
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_boot_gate_fails_open_when_resolution_fails(monkeypatch):
    """(c) Boot: the model check itself fails — the resume proceeds (the turn
    surfaces the resolution failure exactly as it would without the gate)."""
    runner, adapter = make_restart_runner()
    entry = _resume_entry(resume_model=dict(OLD_MODEL))
    runner.session_store._entries = {entry.session_key: entry}
    runner._resolve_session_agent_runtime = MagicMock(
        side_effect=RuntimeError("no credentials")
    )
    monkeypatch.setattr(
        "gateway.session_policy.policy_for_source", lambda *args, **kwargs: None
    )
    adapter.handle_message = AsyncMock()

    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.sleep(0)
    adapter.handle_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# (c) Next-message gate: a real user message runs fresh under the new model
# ---------------------------------------------------------------------------


def _turn_runner_with_store(tmp_path, message: str, *, resume_model=None):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    with store._lock:
        current = store._entries[entry.session_key]
        current.resume_pending = True
        current.resume_reason = "restart_interrupted"
        current.last_resume_marked_at = datetime.now()
        current.resume_model = resume_model
    turn_runner = object.__new__(TurnRunner)
    turn_runner._runner = SimpleNamespace(session_store=store)
    turn_runner._ctx = SimpleNamespace(message=message, session_key=entry.session_key)
    return turn_runner, store, entry


def test_next_message_gate_clears_the_flag_on_model_change(tmp_path):
    """(c) A user message after a model change: the flag is cleared so the turn
    runs fresh (no recovery note), history kept."""
    turn_runner, store, _entry = _turn_runner_with_store(
        tmp_path, "follow up", resume_model=dict(OLD_MODEL)
    )
    agent = SimpleNamespace(model="new-model", provider="openrouter")

    assert turn_runner._gate_resume_on_model_change(agent) is True
    assert _entry_for(store, _make_source()).resume_pending is False
    assert _entry_for(store, _make_source()).resume_model is None


def test_next_message_gate_keeps_the_flag_on_the_same_model(tmp_path):
    turn_runner, store, _entry = _turn_runner_with_store(
        tmp_path, "follow up", resume_model=dict(OLD_MODEL)
    )
    agent = SimpleNamespace(model="old-model", provider="OpenRouter")

    assert turn_runner._gate_resume_on_model_change(agent) is False
    assert _entry_for(store, _make_source()).resume_pending is True


def test_next_message_gate_ignores_empty_boot_turns(tmp_path):
    """(c) Empty-text turns are the boot gate's job: the in-turn gate never
    clears the flag for them (clearing it would leave a blank user turn)."""
    turn_runner, store, _entry = _turn_runner_with_store(
        tmp_path, "", resume_model=dict(OLD_MODEL)
    )
    agent = SimpleNamespace(model="new-model", provider="openrouter")

    assert turn_runner._gate_resume_on_model_change(agent) is False
    assert _entry_for(store, _make_source()).resume_pending is True


def test_stamp_and_gate_round_trip_through_the_runner(tmp_path):
    """(c/d) The stamp the runner takes after agent resolution is the model the
    crash promotion later copies onto the resume marker."""
    turn_runner, store, entry = _turn_runner_with_store(
        tmp_path, "do the thing", resume_model=None
    )
    with store._lock:
        store._entries[entry.session_key].resume_pending = False
    token = store.mark_turn_active(entry.session_key)
    agent = SimpleNamespace(model="old-model", provider="openrouter")

    turn_runner._stamp_active_turn_model(agent)
    assert _entry_for(store, _make_source()).active_turn_model == OLD_MODEL

    # No resume_pending flag: the gate is a no-op for an ordinary turn.
    assert turn_runner._gate_resume_on_model_change(agent) is False
    assert store.clear_turn_active(entry.session_key, token) is True
