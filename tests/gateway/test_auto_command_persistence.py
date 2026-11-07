"""`/auto` must persist like `/yolo` (Phase 4, F1).

`/yolo` writes the session bypass to the routing entry and every turn restores
it, so it survives a gateway restart. `/auto` flips the same in-memory flag but
never wrote the entry: `/auto on` was silently lost on restart, and `/auto off`
after a `/yolo on` was revived by the next process's restore.
"""

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource, SessionStore
from tools.approval import disable_session_yolo, is_session_yolo_enabled
from tools.approval_yolo import restore_session_yolo


@pytest.fixture(autouse=True)
def _clean_yolo_state():
    disable_session_yolo("agent:main:telegram:dm:chat-a")
    yield
    disable_session_yolo("agent:main:telegram:dm:chat-a")


def _store(tmp_path):
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    store._db = None  # JSON routing index: the restart reads it back from disk
    return store


def _make_runner(store):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.session_store = store
    runner.config = None
    return runner


def _make_event(chat_id: str, text: str = "/auto") -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id=f"user-{chat_id}",
        chat_id=chat_id,
        user_name="tester",
        chat_type="dm",
    )
    return MessageEvent(text=text, source=source)


@pytest.mark.asyncio
async def test_auto_on_survives_a_fresh_process(tmp_path):
    store = _store(tmp_path)
    runner = _make_runner(store)
    event = _make_event("chat-a", "/auto")
    source = runner._normalize_source_for_session_key(event.source)
    key = runner._session_key_for_source(source)

    result = await runner._handle_auto_command(event)
    assert "enabled" in str(result).lower()
    assert is_session_yolo_enabled(key) is True
    assert store.get_or_create_session(source).yolo is True

    disable_session_yolo(key)  # a new process starts with an empty in-memory set
    restore_session_yolo(key, store.get_or_create_session(source).yolo)
    assert is_session_yolo_enabled(key) is True


@pytest.mark.asyncio
async def test_auto_off_revokes_a_persisted_yolo(tmp_path):
    store = _store(tmp_path)
    runner = _make_runner(store)
    event_on = _make_event("chat-a", "/yolo")
    key = runner._session_key_for_source(event_on.source)

    await runner._handle_yolo_command(event_on)
    assert is_session_yolo_enabled(key) is True
    assert store.get_or_create_session(event_on.source).yolo is True

    result = await runner._handle_auto_command(_make_event("chat-a", "/auto off"))
    assert "disabled" in str(result).lower()
    assert is_session_yolo_enabled(key) is False
    assert store.get_or_create_session(event_on.source).yolo is False

    # A fresh process's restore must not revive it.
    restore_session_yolo(key, store.get_or_create_session(event_on.source).yolo)
    assert is_session_yolo_enabled(key) is False
