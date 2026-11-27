"""Phase 5 Group B: Telegram rich cards for session commands — /undo, /title,
/compress, /topic, /save, /discard, /resume, /sessions (+ /retry refusals,
/branch confirmations, /search bare help).

Invariant tests, written red-first: on Telegram each handler sends one native
rich card through ``adapter.send_command_card`` and returns None; session lists
carry per-session resume buttons (``hm:resume:<id>``); other platforms keep
plain text.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


def _source(*, platform: Platform = Platform.TELEGRAM, chat_type: str = "dm") -> SessionSource:
    return SessionSource(
        platform=platform, user_id="u1", chat_id="c1", user_name="tester", chat_type=chat_type,
    )


def _event(text: str, *, platform: Platform = Platform.TELEGRAM,
           chat_type: str = "dm") -> MessageEvent:
    return MessageEvent(text=text, source=_source(platform=platform, chat_type=chat_type),
                        message_id="m1")


class _Store:
    """Minimal async session store; ``_store`` must be the runner's session_store."""

    def __init__(self, store=None, history=None, rewind=None):
        self._store = store
        self._history = history if history is not None else []
        self._rewind = rewind

    async def get_or_create_session(self, source):
        return SimpleNamespace(
            session_key="agent:main:telegram:dm:u1", session_id="sess-1",
            created_at=datetime.now(), updated_at=datetime.now(),
            platform=Platform.TELEGRAM, chat_type="dm", total_tokens=0,
            last_prompt_tokens=0,
        )

    async def load_transcript(self, session_id):
        return list(self._history)

    async def rewind_session(self, session_id, n):
        return self._rewind


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    adapter = SimpleNamespace(send=AsyncMock(), send_command_card=AsyncMock(return_value=True))
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._delivery_adapter_for = lambda source: adapter
    runner._reply_anchor_for_event = lambda event: None
    runner._session_key_for_source = lambda source: "agent:main:telegram:dm:u1"
    runner.session_store = MagicMock()
    runner._async_session_store = _Store(store=runner.session_store)
    runner._session_db = AsyncMock()
    runner._session_db.get_session_title.return_value = None
    return runner, adapter


def _card_kwargs(adapter):
    assert adapter.send_command_card.await_args is not None, "expected a Telegram rich card"
    return adapter.send_command_card.await_args.kwargs


# --- pure button builders ------------------------------------------------------

def test_command_card_markup_builds_explicit_buttons_only():
    from plugins.platforms.telegram.command_cards import command_card_markup

    markup = command_card_markup(buttons=(("Alpha", "hm:resume:sess-a"),))
    rows = markup.rows
    assert len(rows) == 1
    assert rows[0].buttons[0].text == "Alpha"
    assert rows[0].buttons[0].type.data == b"hm:resume:sess-a"
    assert command_card_markup() is None  # no buttons, no nav: no keyboard at all


# --- /undo ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_undo_invalid_count_sends_rich_card():
    runner, adapter = _make_runner()
    result = await runner._handle_undo_command(_event("/undo abc"))
    assert result is None
    assert "Invalid" in _card_kwargs(adapter)["html"] or "invalid" in _card_kwargs(adapter)["html"]


@pytest.mark.asyncio
async def test_undo_nothing_sends_rich_card():
    runner, adapter = _make_runner()
    runner._async_session_store = _Store(store=runner.session_store, rewind=None)
    result = await runner._handle_undo_command(_event("/undo"))
    assert result is None
    adapter.send_command_card.assert_awaited_once()


# --- /title --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_title_current_no_title_sends_rich_card():
    runner, adapter = _make_runner()
    result = await runner._handle_title_command(_event("/title"))
    assert result is None
    kwargs = _card_kwargs(adapter)
    assert "sess-1" in kwargs["html"]
    assert ("Copy session id", "sess-1") in kwargs["copies"]


# --- /compress -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_compress_not_enough_sends_rich_card():
    runner, adapter = _make_runner()
    runner.config = SimpleNamespace(multiplex_profiles=False)
    result = await runner._handle_compress_command(_event("/compress"))
    assert result is None
    adapter.send_command_card.assert_awaited_once()
    assert "nav" not in _card_kwargs(adapter)  # hm:run:/ nav buttons removed


# --- /topic --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_topic_group_chat_sends_rich_card():
    runner, adapter = _make_runner()
    result = await runner._handle_topic_command(_event("/topic", chat_type="group"))
    assert result is None
    adapter.send_command_card.assert_awaited_once()


# --- /save ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_bare_sends_usage_rich_card():
    runner, adapter = _make_runner()
    result = await runner._handle_save_command(_event("/save"))
    assert result is None
    kwargs = _card_kwargs(adapter)
    assert "save" in kwargs["html"].lower()
    assert "nav" not in kwargs  # hm:run:/ nav buttons removed


# --- /discard ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discard_unauthorized_sends_rich_card():
    runner, adapter = _make_runner()
    runner._is_user_authorized_for_source = lambda source: False
    result = await runner._handle_discard_command(_event("/discard"))
    assert result is None
    adapter.send_command_card.assert_awaited_once()


# --- /resume -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resume_bare_listing_sends_rich_collapsibles():
    runner, adapter = _make_runner()
    runner._normalize_source_for_session_key = lambda source: source
    runner._session_db.list_sessions_rich = AsyncMock(return_value=[
        {"id": "sess-a", "title": "Alpha", "preview": "hello"},
        {"id": "sess-b", "title": "Beta", "preview": ""},
    ])
    runner._resume_row_visible = AsyncMock(return_value=True)
    runner._resume_caller_is_admin = lambda source: False
    result = await runner._handle_resume_command(_event("/resume"))
    assert result is None
    kwargs = _card_kwargs(adapter)
    html = kwargs["html"]
    assert "Alpha" in html and "Beta" in html
    assert "<details>" in html and "<summary>1. Alpha</summary>" in html
    assert "hm:resume:sess-a" in html and "hm:rename:sess-a" in html
    assert "<i>hello</i>" in html and "_hello_" not in html  # real italics, not literal markers
    assert not kwargs.get("buttons")  # no regular buttons on the listing
    assert kwargs["copies"] == ()  # hint copy pills removed from the listing


# --- /sessions ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sessions_listing_sends_rich_collapsibles(monkeypatch):
    import hermes_cli.session_listing as listing

    runner, adapter = _make_runner()
    runner._normalize_source_for_session_key = lambda source: source
    runner._resume_row_visible = AsyncMock(return_value=True)
    runner._resume_caller_is_admin = lambda source: False
    rows = [
        {"id": "sess-a", "title": "Alpha", "preview": "hello"},
        {"id": "sess-b", "title": "Beta", "preview": ""},
    ]
    monkeypatch.setattr(listing, "query_session_listing", lambda *a, **k: list(rows))
    monkeypatch.setattr(listing, "format_gateway_session_listing",
                        lambda rows, **k: "Sessions\n1. Alpha\n2. Beta")
    result = await runner._handle_sessions_command(_event("/sessions"))
    assert result is None
    kwargs = _card_kwargs(adapter)
    html = kwargs["html"]
    assert "<details>" in html and "<summary>Alpha</summary>" in html
    assert "hm:resume:sess-a" in html and "hm:rename:sess-a" in html
    assert "<i>hello</i>" in html and "_hello_" not in html  # real italics, not literal markers
    assert not kwargs.get("buttons")  # no regular buttons on the listing
    assert kwargs["copies"] == ()  # hint copy pills removed from the listing
    assert "nav" not in kwargs  # hm:run:/ nav buttons removed: no navigation row


# --- hm:resume: callback ---------------------------------------------------------

def _callback_adapter():
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    adapter = object.__new__(TelethonAdapter)
    adapter.handle_message = AsyncMock()
    return adapter


@pytest.mark.asyncio
async def test_resume_callback_dispatches_resume_for_session_id():
    adapter = _callback_adapter()
    source = SimpleNamespace(chat_id="c1", user_id="u1", user_name="tester", thread_id=None)
    event = SimpleNamespace(answer=AsyncMock(), message_id=77)
    await adapter._handle_resume_callback(event, source, "hm:resume:sess-a")
    adapter.handle_message.assert_awaited_once()
    dispatched = adapter.handle_message.await_args.args[0]
    assert dispatched.text == "/resume sess-a"
    assert dispatched.source is source


@pytest.mark.asyncio
async def test_resume_callback_refuses_empty_session_id():
    adapter = _callback_adapter()
    source = SimpleNamespace(chat_id="c1", user_id="u1", user_name="tester", thread_id=None)
    event = SimpleNamespace(answer=AsyncMock(), message_id=77)
    await adapter._handle_resume_callback(event, source, "hm:resume:")
    adapter.handle_message.assert_not_awaited()
    event.answer.assert_awaited_once()


# --- /search bare help -----------------------------------------------------------

@pytest.mark.asyncio
async def test_search_help_sends_rich_card():
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    adapter = object.__new__(TelethonAdapter)
    adapter._send_rich_ui = AsyncMock()
    source = SimpleNamespace(chat_id="c1", user_id="u1", thread_id=None)
    await adapter._show_search_help(source, reply_to=5)
    adapter._send_rich_ui.assert_awaited_once()
    html = adapter._send_rich_ui.await_args.args[1]
    assert "Web search" in html
    assert "/search" in html
    markup = adapter._send_rich_ui.await_args.args[2]
    assert markup is None  # no nav buttons: search help carries copy pills, not a keyboard
