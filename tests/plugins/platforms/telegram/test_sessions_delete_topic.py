"""Sessions Delete tapped in a Telegram forum topic: the confirm card must arm.

Regression (2026-11-24, live report): tapping Delete on /sessions did nothing —
the button stayed pressed until Telegram timed it out, no answer, no card.
Two stacked causes: (1) the arm read the session through
``_session_db.get_session``, which does not exist on the Persistence handle, so
the AttributeError died in the adapter's outer ``except`` before any answer;
(2) even with the lookup fixed, a topic tap's source carries the General
fallback thread (``_source`` sees no message on a CallbackQuery) while the
session origin carries the real topic id, so the IDOR guard denied the tap.
The handler must read the thread off the tapped card's own message, as the
/new-confirm fix does — and a tap from a different topic must still be denied.
"""

from pathlib import Path as _Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource


def _topic_source(thread_id):
    return SessionSource(
        platform=Platform.TELEGRAM, user_id="7", chat_id="-1001",
        user_name="tester", chat_type="forum", thread_id=thread_id)


def _card_message(thread_id):
    return SimpleNamespace(
        reply_to=SimpleNamespace(reply_to_top_id=thread_id, forum_topic=True))


def _markup_payloads(request):
    out = []
    for row in request.reply_markup.rows:
        for button in row.buttons:
            out.append(button.type.data.decode("utf-8"))
    return out


def _live_runner(tmp_path, monkeypatch):
    """Runner with a real store holding a live session in topic 123, titled."""
    from gateway.run import GatewayRunner
    from gateway.session import SessionStore

    home = tmp_path / ".hermes"
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = SessionStore(home / "sessions", runner.config)
    entry = runner.session_store.get_or_create_session(_topic_source("123"))
    return runner, entry.session_id


@pytest.mark.asyncio
async def test_delete_tap_in_topic_arms_confirm_card(tmp_path, monkeypatch):
    from plugins.platforms.telegram.hm_action_callbacks import handle_delete_callback
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    runner, session_id = _live_runner(tmp_path, monkeypatch)
    handle = runner.session_store._scoped_persistence()
    assert handle is not None
    try:
        await handle.aio.sessions.set_title(session_id, "Hey #2")
        runner._session_db = handle

        adapter = object.__new__(TelethonAdapter)
        adapter.gateway_runner = runner
        adapter._peer_for = lambda chat_id: chat_id
        adapter._mtproto_rich_request = AsyncMock()
        adapter._is_owner_user = lambda user_id, source=None: True

        event = SimpleNamespace(
            answer=AsyncMock(), message_id=77,
            get_message=AsyncMock(return_value=_card_message(123)))
        # The live tap source: _source sees no message on a CallbackQuery, so
        # it falls back to the General topic (thread_id=1).
        await handle_delete_callback(
            adapter, event, _topic_source("1"),
            f"hm:delete:tok12345:1:{session_id}")
        event.answer.assert_awaited_once_with()
        adapter._mtproto_rich_request.assert_awaited_once()
        request = adapter._mtproto_rich_request.await_args.args[0]
        assert "Hey #2" in request.message
        assert any(p.startswith("hm:delete:confirm:") for p in _markup_payloads(request))
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_delete_tap_from_other_topic_still_denied(tmp_path, monkeypatch):
    from plugins.platforms.telegram.hm_action_callbacks import handle_delete_callback
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    runner, session_id = _live_runner(tmp_path, monkeypatch)
    handle = runner.session_store._scoped_persistence()
    assert handle is not None
    try:
        await handle.aio.sessions.set_title(session_id, "Hey #2")
        runner._session_db = handle

        adapter = object.__new__(TelethonAdapter)
        adapter.gateway_runner = runner
        adapter._peer_for = lambda chat_id: chat_id
        adapter._mtproto_rich_request = AsyncMock()
        adapter._is_owner_user = lambda user_id, source=None: True

        event = SimpleNamespace(
            answer=AsyncMock(), message_id=77,
            get_message=AsyncMock(return_value=_card_message(999)))
        await handle_delete_callback(
            adapter, event, _topic_source("1"),
            f"hm:delete:tok12345:1:{session_id}")
        event.answer.assert_awaited_once()
        assert event.answer.await_args.kwargs.get("alert") is True
        assert "cannot be deleted" in (event.answer.await_args.args[0] or "").lower()
        adapter._mtproto_rich_request.assert_not_awaited()
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_rename_tap_in_topic_prompts_for_title(tmp_path, monkeypatch):
    """Rename armed through the same dead ``get_session`` lookup and the same
    General-fallback thread, so a topic tap died the same silent way."""
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    runner, session_id = _live_runner(tmp_path, monkeypatch)
    handle = runner.session_store._scoped_persistence()
    assert handle is not None
    try:
        await handle.aio.sessions.set_title(session_id, "Hey #2")
        runner._session_db = handle

        adapter = object.__new__(TelethonAdapter)
        adapter.gateway_runner = runner
        adapter._send_rich_ui = AsyncMock()

        event = SimpleNamespace(
            answer=AsyncMock(), message_id=77,
            get_message=AsyncMock(return_value=_card_message(123)))
        await adapter._handle_rename_callback(
            event, _topic_source("1"), f"hm:rename:{session_id}")
        event.answer.assert_awaited_once_with()
        adapter._send_rich_ui.assert_awaited_once()
        assert "Hey #2" in adapter._send_rich_ui.await_args.args[1]
    finally:
        runner.session_store.close_all_db_handles()
