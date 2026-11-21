"""Behaviour contracts for the B2B numeric handshake spine.

The codes live only in code: these tests use the numeric values with minimal
prose, and assert the receipt/send behaviour around them.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.session import SessionSource, build_session_key
from plugins.platforms.telegram.b2b_handshake import (
    CODE_ERROR,
    CODE_GREETING,
    CODE_LEAVE,
    CODE_SECURE,
    arm_talk_closed_note,
    evaluate_peer_trigger,
    live_talk_record,
    note_sent_code,
    parse_code,
    peer_settings,
    route_peer_message,
    take_talk_closed_note,
    talk_session_key,
)
from plugins.platforms.telegram.bot_operations import BotOperationsStore
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

PEER = 42
GROUP = "-100123"


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=100\n")
    return home


def _adapter(home: Path) -> TelethonAdapter:
    adapter = TelethonAdapter(PlatformConfig())
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda source: home,
        async_session_store=SimpleNamespace(set_model_override=AsyncMock()),
    )
    adapter._authorized = lambda _source: True
    adapter._bot_username = "NainaAssistantBot"
    adapter._bot_id = 999
    adapter._chat_settings = lambda _source: {}
    adapter._session_key_profile = lambda _source: None
    adapter.send = AsyncMock()
    return adapter


def _peer_source(home: Path, *, chat_type="dm", chat_id="42", user_id=PEER):
    return SimpleNamespace(
        home=home, user_id=str(user_id), chat_id=str(chat_id), chat_type=chat_type,
        thread_id=None, is_bot=True, username="peer42_bot", display_name="Peer Bot",
        profile=None,
    )


def _event(source, text, *, message_id=55):
    return SimpleNamespace(
        source=source,
        message=SimpleNamespace(
            message=text, id=message_id, reply_to_msg_id=None,
            get_reply_message=AsyncMock(return_value=None),
        ),
        raw_text=text,
    )


def _add_peer(home: Path, **settings):
    store = BotOperationsStore(home)
    store.add_peer({"id": PEER, "username": "peer42_bot"},
                   group_enabled=True, dm_enabled=True, group_chat_id=GROUP)
    if settings:
        store.set_peer_settings(PEER, settings)
    return store


# --- parsing -----------------------------------------------------------------


def test_bare_codes_parse_and_anything_else_does_not():
    assert parse_code(str(CODE_GREETING)) == (CODE_GREETING, None)
    assert parse_code(str(CODE_ERROR)) == (CODE_ERROR, None)
    assert parse_code(str(CODE_SECURE)) == (CODE_SECURE, None)
    assert parse_code(f"{CODE_LEAVE} {GROUP}") == (CODE_LEAVE, GROUP)
    assert parse_code(str(CODE_LEAVE)) == (CODE_LEAVE, None)
    assert parse_code("hello") is None
    assert parse_code(f"{CODE_GREETING} hello") is None
    assert parse_code(f"say {CODE_GREETING}") is None
    assert parse_code("1234") is None
    assert parse_code(f"{CODE_LEAVE} not-a-chat") is None
    assert parse_code(f"@{ 'NainaAssistantBot' } {CODE_GREETING}",
                      bot_username="NainaAssistantBot") == (CODE_GREETING, None)


# --- receipt -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_greeting_receipt_answers_with_a_greeting(tmp_path):
    home = _home(tmp_path)
    _add_peer(home)
    adapter = _adapter(home)
    source = _peer_source(home)
    turn, _note = await route_peer_message(adapter, _event(source, str(CODE_GREETING)), source, str(CODE_GREETING))
    assert turn is False
    adapter.send.assert_awaited_once()
    assert str(CODE_GREETING) in adapter.send.await_args.args[1]


@pytest.mark.asyncio
async def test_error_receipt_answers_with_an_error(tmp_path):
    home = _home(tmp_path)
    _add_peer(home)
    adapter = _adapter(home)
    source = _peer_source(home)
    turn, _note = await route_peer_message(adapter, _event(source, str(CODE_ERROR)), source, str(CODE_ERROR))
    assert turn is False
    adapter.send.assert_awaited_once()
    assert str(CODE_ERROR) in adapter.send.await_args.args[1]


@pytest.mark.asyncio
async def test_secure_receipt_creates_a_live_talk_when_allowed(tmp_path):
    home = _home(tmp_path)
    store = _add_peer(home)
    adapter = _adapter(home)
    source = _peer_source(home)
    turn, _note = await route_peer_message(adapter, _event(source, str(CODE_SECURE)), source, str(CODE_SECURE))
    assert turn is False
    talk = store.get_talk(PEER)
    assert talk is not None and talk["state"] == "live" and talk["killed"] is False


@pytest.mark.asyncio
async def test_secure_receipt_is_ignored_when_blocked(tmp_path):
    home = _home(tmp_path)
    store = _add_peer(home, secure="blocked")
    adapter = _adapter(home)
    source = _peer_source(home)
    turn, _note = await route_peer_message(adapter, _event(source, str(CODE_SECURE)), source, str(CODE_SECURE))
    assert turn is False
    assert store.get_talk(PEER) is None


@pytest.mark.asyncio
async def test_secure_receipt_never_resurrects_a_killed_talk(tmp_path):
    home = _home(tmp_path)
    store = _add_peer(home)
    store.upsert_talk(PEER, live_talk_record(PEER))
    store.mark_talk_killed(PEER)
    adapter = _adapter(home)
    source = _peer_source(home)
    turn, _note = await route_peer_message(adapter, _event(source, str(CODE_SECURE)), source, str(CODE_SECURE))
    assert turn is False
    talk = store.get_talk(PEER)
    assert talk["state"] == "killed" and talk["killed"] is True


@pytest.mark.asyncio
async def test_leave_stops_a_live_talk_and_fails_without_a_chat_id(tmp_path):
    home = _home(tmp_path)
    store = _add_peer(home)
    store.upsert_talk(PEER, live_talk_record(PEER))
    adapter = _adapter(home)
    source = _peer_source(home)
    turn, _note = await route_peer_message(
        adapter, _event(source, str(CODE_LEAVE)), source, str(CODE_LEAVE))
    assert turn is False
    assert store.get_talk(PEER)["state"] == "live"
    turn, _note = await route_peer_message(
        adapter, _event(source, f"{CODE_LEAVE} {GROUP}"), source, f"{CODE_LEAVE} {GROUP}")
    assert turn is False
    talk = store.get_talk(PEER)
    assert talk["state"] == "stopped" and talk["killed"] is False


# --- normal routing ----------------------------------------------------------


@pytest.mark.asyncio
async def test_unadmitted_peer_is_dropped_silently(tmp_path):
    home = _home(tmp_path)
    adapter = _adapter(home)
    source = _peer_source(home)
    turn, _note = await route_peer_message(adapter, _event(source, "hello"), source, "hello")
    assert (turn, _note) == (False, "")
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_admitted_peer_turn_stamps_admission_on_the_event(tmp_path):
    home = _home(tmp_path)
    _add_peer(home, notify="off")
    adapter = _adapter(home)
    source = _peer_source(home)
    event = _event(source, "hello")
    turn, _note = await route_peer_message(adapter, event, source, "hello")
    assert turn is True
    assert event._b2b_admitted is True
    assert getattr(source, "b2b_session_key", None) == talk_session_key(None, PEER)


@pytest.mark.asyncio
async def test_live_talk_bypasses_the_guard_flag_on_the_event(tmp_path):
    home = _home(tmp_path)
    store = _add_peer(home, notify="off")
    store.upsert_talk(PEER, live_talk_record(PEER))
    adapter = _adapter(home)
    source = _peer_source(home)
    event = _event(source, "hello")
    turn, _note = await route_peer_message(adapter, event, source, "hello")
    assert turn is True
    assert event._b2b_secure is True


@pytest.mark.asyncio
async def test_inbox_intake_never_starts_a_turn(tmp_path):
    home = _home(tmp_path)
    store = _add_peer(home, intake="inbox")
    adapter = _adapter(home)
    source = _peer_source(home)
    turn, _note = await route_peer_message(adapter, _event(source, "hello"), source, "hello")
    assert turn is False
    assert len(store.inbox(limit=50)) == 1


@pytest.mark.asyncio
async def test_short_acceptance_replies_and_starts_no_turn(tmp_path):
    home = _home(tmp_path)
    _add_peer(home, acceptance="short", acceptance_text="noted", notify="off")
    adapter = _adapter(home)
    source = _peer_source(home)
    turn, _note = await route_peer_message(adapter, _event(source, "hello"), source, "hello")
    assert turn is False
    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.args[1] == "noted"


@pytest.mark.asyncio
async def test_peer_model_is_applied_to_the_talk_session_key(tmp_path):
    home = _home(tmp_path)
    _add_peer(home, model="some-model", notify="off")
    adapter = _adapter(home)
    source = _peer_source(home)
    turn, _note = await route_peer_message(adapter, _event(source, "hello"), source, "hello")
    assert turn is True
    adapter.gateway_runner.async_session_store.set_model_override.assert_awaited_once_with(
        talk_session_key(None, PEER), {"model": "some-model"})


@pytest.mark.asyncio
async def test_null_peer_model_clears_any_override(tmp_path):
    home = _home(tmp_path)
    _add_peer(home, notify="off")
    adapter = _adapter(home)
    source = _peer_source(home)
    turn, _note = await route_peer_message(adapter, _event(source, "hello"), source, "hello")
    assert turn is True
    adapter.gateway_runner.async_session_store.set_model_override.assert_awaited_once_with(
        talk_session_key(None, PEER), None)


# --- trigger ladder ----------------------------------------------------------


def test_peer_trigger_ladder():
    assert evaluate_peer_trigger("mention", mentioned=True, reply_to_own=False, word_trigger=False) is True
    assert evaluate_peer_trigger("mention", mentioned=False, reply_to_own=True, word_trigger=False) is True
    assert evaluate_peer_trigger("mention", mentioned=False, reply_to_own=False, word_trigger=True) is False
    assert evaluate_peer_trigger("word", mentioned=False, reply_to_own=False, word_trigger=True) is True
    assert evaluate_peer_trigger("word", mentioned=False, reply_to_own=False, word_trigger=False) is False
    assert evaluate_peer_trigger("direct", mentioned=False, reply_to_own=False, word_trigger=False) is True


@pytest.mark.asyncio
async def test_group_peer_without_addressing_is_dropped(tmp_path):
    home = _home(tmp_path)
    _add_peer(home, notify="off")
    adapter = _adapter(home)
    source = _peer_source(home, chat_type="group", chat_id=GROUP)
    turn, _note = await route_peer_message(adapter, _event(source, "hello"), source, "hello")
    assert turn is False
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_peer_with_a_mention_routes_to_a_turn(tmp_path):
    home = _home(tmp_path)
    _add_peer(home, notify="off")
    adapter = _adapter(home)
    source = _peer_source(home, chat_type="group", chat_id=GROUP)
    text = "@NainaAssistantBot hello"
    turn, _note = await route_peer_message(adapter, _event(source, text), source, text)
    assert turn is True


# --- sender side -------------------------------------------------------------


@pytest.mark.asyncio
async def test_sending_the_agreement_marks_the_talk_live(tmp_path):
    home = _home(tmp_path)
    store = _add_peer(home)
    adapter = _adapter(home)
    adapter._telegram_transport_home = home
    await note_sent_code(adapter, "42", str(CODE_SECURE))
    talk = store.get_talk(PEER)
    assert talk is not None and talk["state"] == "live"


@pytest.mark.asyncio
async def test_sending_anything_else_marks_nothing(tmp_path):
    home = _home(tmp_path)
    store = _add_peer(home)
    adapter = _adapter(home)
    adapter._telegram_transport_home = home
    await note_sent_code(adapter, "42", "hello")
    await note_sent_code(adapter, "42", str(CODE_GREETING))
    assert store.get_talk(PEER) is None


# --- closed note + session key -----------------------------------------------


def test_closed_note_is_one_shot_and_session_scoped(tmp_path):
    home = _home(tmp_path)
    key = talk_session_key(None, PEER)
    arm_talk_closed_note(home, key, PEER)
    note = take_talk_closed_note(home, key)
    assert note is not None and note["peer_id"] == PEER
    assert take_talk_closed_note(home, key) is None
    assert take_talk_closed_note(home, "agent:main:telegram:b2b:77") is None


def test_talk_key_is_the_session_key_for_a_marked_source():
    key = talk_session_key(None, PEER)
    assert key == "agent:main:telegram:b2b:42"
    source = SessionSource(platform="telegram", chat_id="42", chat_type="dm",
                           user_id="42", is_bot=True, b2b_session_key=key)
    assert build_session_key(source) == key
    assert peer_settings({})["intake"] == "turns"
