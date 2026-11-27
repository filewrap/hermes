"""Guest history clear: arm counts the guest sessions, confirm deletes them.

Invariant tests, written red-first. Clearing deletes ONLY guest sessions —
an ordinary DM or group session in the same profile is untouched. A live
guest session is skipped, never deleted. Each deleted session's cached agent
is evicted, or its in-memory history would resurrect the deleted session on
the next turn through the cached-history lag guard.
"""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from persistence import SessionWriteGuarded


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", user_name="tester",
        chat_type="dm",
    )


def _entry(session_id, *, thread_id, session_key, message_count):
    return SimpleNamespace(session=SimpleNamespace(
        session_id=session_id, thread_id=thread_id,
        session_key=session_key, message_count=message_count))


def _guest_entries():
    return [
        _entry("guest-1", thread_id="guest-77",
               session_key="agent:main:telegram:dm:88:guest-77", message_count=3),
        _entry("guest-2", thread_id="guest-99",
               session_key="agent:main:telegram:group:55:guest-99", message_count=5),
        _entry("owner-dm", thread_id="1234",
               session_key="agent:main:telegram:dm:u1", message_count=40),
        _entry("group", thread_id="7",
               session_key="agent:main:telegram:group:55", message_count=12),
    ]


def _make_runner(entries):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._session_db = AsyncMock()
    runner._session_db.state_meta.get = MagicMock(return_value=None)
    runner._session_db.state_meta.set = MagicMock()
    # The listing reads through the pinned ``_session_db`` handle (the
    # Persistence handle forwards ``aio``); pinning it would make the
    # ``persistence`` property read None, so the fake listing lives here.
    runner._session_db.aio.sessions.list_sessions = AsyncMock(return_value=list(entries))
    runner.session_store = MagicMock()
    runner._resolve_profile_home_for_source = lambda source: "/tmp/guest-clear-home"
    cancelled = []
    runner._cancel_wakeups_for_session = lambda sid: cancelled.append(sid)
    evicted = []
    runner._evict_cached_agent = lambda key: evicted.append(key)
    runner._cancelled_wakeups = cancelled
    runner._evicted_keys = evicted
    return runner


@pytest.mark.asyncio
async def test_arm_counts_guest_sessions_only():
    runner = _make_runner(_guest_entries())
    armed = await runner.arm_guest_history_clear(_source())
    assert armed is not None
    token, session_count, message_count = armed
    assert session_count == 2
    assert message_count == 8
    assert token in runner._guest_clear_confirms()


@pytest.mark.asyncio
async def test_arm_with_no_guest_sessions_counts_zero():
    runner = _make_runner([_guest_entries()[2]])
    armed = await runner.arm_guest_history_clear(_source())
    assert armed is not None
    _token, session_count, message_count = armed
    assert (session_count, message_count) == (0, 0)


@pytest.mark.asyncio
async def test_confirm_deletes_only_guest_sessions(monkeypatch, tmp_path):
    from pathlib import Path

    runner = _make_runner(_guest_entries())
    runner._resolve_profile_home_for_source = lambda source: tmp_path
    token, _n, _m = await runner.arm_guest_history_clear(_source())
    calls = []

    def fake_delete(store, session_id, sessions_dir):
        calls.append((session_id, sessions_dir))
        return True

    monkeypatch.setattr("gateway.platforms.api_server_store.delete_session", fake_delete)
    monkeypatch.setattr("gateway.session_ingress_media.collect_unheld_api_images",
                        lambda store: None)

    outcome = await runner.clear_guest_history_confirmed(_source(), token)

    assert outcome["deleted_sessions"] == 2
    assert outcome["deleted_messages"] == 8
    assert outcome["skipped"] == 0
    assert [c[0] for c in calls] == ["guest-1", "guest-2"]
    assert calls[0][1] == tmp_path / "sessions"
    assert runner._cancelled_wakeups == ["guest-1", "guest-2"]
    runner.session_store.remove_by_session_id.assert_any_call("guest-1")
    runner.session_store.remove_by_session_id.assert_any_call("guest-2")
    assert runner.session_store.remove_by_session_id.call_count == 2
    assert runner._evicted_keys == [
        "agent:main:telegram:dm:88:guest-77",
        "agent:main:telegram:group:55:guest-99"]


@pytest.mark.asyncio
async def test_confirm_skips_a_live_guest_session(monkeypatch, tmp_path):
    runner = _make_runner(_guest_entries())
    runner._resolve_profile_home_for_source = lambda source: tmp_path
    token, _n, _m = await runner.arm_guest_history_clear(_source())

    def fake_delete(store, session_id, sessions_dir):
        if session_id == "guest-1":
            raise SessionWriteGuarded("live")
        return True

    monkeypatch.setattr("gateway.platforms.api_server_store.delete_session", fake_delete)
    monkeypatch.setattr("gateway.session_ingress_media.collect_unheld_api_images",
                        lambda store: None)

    outcome = await runner.clear_guest_history_confirmed(_source(), token)

    assert outcome["deleted_sessions"] == 1
    assert outcome["deleted_messages"] == 5
    assert outcome["skipped"] == 1
    assert runner._cancelled_wakeups == ["guest-2"]
    assert runner._evicted_keys == ["agent:main:telegram:group:55:guest-99"]


@pytest.mark.asyncio
async def test_confirm_with_unknown_token_returns_none():
    runner = _make_runner(_guest_entries())
    assert await runner.clear_guest_history_confirmed(_source(), "no-such-token") is None
