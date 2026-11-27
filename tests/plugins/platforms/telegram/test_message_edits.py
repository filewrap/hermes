"""Message edits (beta): an edited message becomes a new turn.

When the beta toggle is on, a turn response is remembered as the reply to the
user message it answered, so a later edit of that message can correct the
reply in place. When it is off, nothing is remembered. Only sends marked as
turn responses are recorded; status/working sends never land in the cache,
and a failed send drops the entry.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from plugins.platforms.telegram.message_edits import (
    ACK_TEXT,
    clear_reply,
    edit_revision_for,
    message_edits_enabled,
    previous_reply_id_for,
    record_reply,
)
from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


FLAG = {"telegram_threaded_reply": True}
CHAT = "42"
ANCHOR = "400"


class _Client:
    """Fake MTProto client returning incrementing message ids; optionally fails one call."""

    def __init__(self, *, fail_on: tuple[int, ...] = ()):
        self.requests: list = []
        self._next_id = 900
        self._fail_on = set(fail_on)

    async def __call__(self, request):
        self.requests.append(request)
        if len(self.requests) in self._fail_on:
            raise RuntimeError("send failed")
        message_id = self._next_id
        self._next_id += 1
        return SimpleNamespace(id=message_id)


def _adapter(*, edits: bool = False) -> TelethonAdapter:
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = _Client()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(42, 0))
    if edits:
        adapter._message_edits_runtime = True
    return adapter


def test_message_edits_default_is_off():
    """A fresh adapter (no config key, no runtime flag) has message edits off."""
    assert message_edits_enabled(TelethonAdapter(PlatformConfig())) is False


def test_message_edits_reads_profile_config_extra():
    """The persisted profile key turns message edits on without a live toggle."""
    adapter = TelethonAdapter(PlatformConfig(extra={"message_edits": True}))
    assert message_edits_enabled(adapter) is True


def test_message_edits_runtime_flag_wins_over_config():
    """The live toggle (set by /settings) wins over the persisted config value."""
    adapter = TelethonAdapter(PlatformConfig(extra={"message_edits": True}))
    adapter._message_edits_runtime = False
    assert message_edits_enabled(adapter) is False


def test_reply_cache_records_and_returns_the_response_id():
    """A recorded reply is found again for the same (chat, user message)."""
    adapter = _adapter()
    assert previous_reply_id_for(adapter, CHAT, ANCHOR) is None
    record_reply(adapter, CHAT, ANCHOR, "900")
    assert previous_reply_id_for(adapter, CHAT, ANCHOR) == "900"


def test_reply_cache_is_keyed_by_chat_and_user_message():
    """The same user message id in another chat is a different entry."""
    adapter = _adapter()
    record_reply(adapter, CHAT, ANCHOR, "900")
    assert previous_reply_id_for(adapter, "43", ANCHOR) is None
    assert previous_reply_id_for(adapter, CHAT, "401") is None


def test_reply_cache_clear_drops_the_entry():
    """Clearing a recorded reply leaves nothing for a later edit to correct."""
    adapter = _adapter()
    record_reply(adapter, CHAT, ANCHOR, "900")
    clear_reply(adapter, CHAT, ANCHOR)
    assert previous_reply_id_for(adapter, CHAT, ANCHOR) is None


def test_reply_cache_expires_after_its_ttl():
    """A reply recorded long ago is gone: the correction goes out as a new message."""
    adapter = _adapter()
    record_reply(adapter, CHAT, ANCHOR, "900")
    store = adapter._message_edit_replies
    reply_id, stamp = store[(CHAT, ANCHOR)]
    store[(CHAT, ANCHOR)] = (reply_id, stamp - 3600)
    assert previous_reply_id_for(adapter, CHAT, ANCHOR) is None


def test_edit_revision_is_stable_per_edit_and_unique_per_date():
    """The same message + edit date gives the same revision; a later edit differs."""
    first = SimpleNamespace(id=400, edit_date=datetime(2026, 11, 23, 10, 10, 12, tzinfo=timezone.utc))
    second = SimpleNamespace(id=400, edit_date=datetime(2026, 11, 23, 10, 10, 22, tzinfo=timezone.utc))
    assert edit_revision_for(first) == edit_revision_for(first)
    assert edit_revision_for(first) != edit_revision_for(second)
    assert edit_revision_for(first).startswith("400:")


def test_edit_revision_without_an_edit_date_still_identifies_the_message():
    """A message with no edit date still yields a revision for its id."""
    assert edit_revision_for(SimpleNamespace(id=400, edit_date=None)).startswith("400:")


@pytest.mark.asyncio
async def test_turn_response_send_is_remembered_for_a_later_edit():
    """A flagged turn response is recorded as the reply to the user message."""
    adapter = _adapter(edits=True)
    result = await adapter.send(CHAT, "the host is down", reply_to=ANCHOR, metadata=dict(FLAG))
    assert result.success
    assert previous_reply_id_for(adapter, CHAT, ANCHOR) == "900"


@pytest.mark.asyncio
async def test_unflagged_sends_are_not_remembered():
    """Working/status sends carry no flag: they never become a correctable reply."""
    adapter = _adapter(edits=True)
    result = await adapter.send(CHAT, "working on it", reply_to=ANCHOR, metadata={})
    assert result.success
    assert previous_reply_id_for(adapter, CHAT, ANCHOR) is None


@pytest.mark.asyncio
async def test_failed_send_drops_the_recorded_reply():
    """A failed turn response leaves no reply id for a later edit to correct."""
    adapter = _adapter(edits=True)
    adapter._client = _Client(fail_on=(1,))
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(42, 0))
    record_reply(adapter, CHAT, ANCHOR, "900")
    result = await adapter.send(CHAT, "the host is down", reply_to=ANCHOR, metadata=dict(FLAG))
    assert not result.success
    assert previous_reply_id_for(adapter, CHAT, ANCHOR) is None


def test_ack_text_is_the_short_confirmation():
    """The edit ack is one short sentence, as ordered."""
    assert ACK_TEXT == "I saw your edit."


# ── Settings card ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Source:
    chat_id: str = "123"
    user_id: str = "123"
    user_name: str = "Test User"
    thread_id: str | None = None
    chat_type: str = "dm"
    profile: str | None = None


class CallbackEvent:
    def __init__(self, message_id):
        self.message_id = message_id
        self.answers = []
        self.answer_kwargs = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text or "")
        self.answer_kwargs.append(kwargs)


class FakeAdapter:
    def __init__(self):
        self._client = object()
        self.edits = []
        self._next_message_id = 100

    def _peer_for(self, chat_id):
        return chat_id

    async def _mtproto_rich_request(self, request):
        self.edits.append(request)
        self._next_message_id += 1
        return SimpleNamespace(id=self._next_message_id)

    @staticmethod
    def _sent_message_id(response):
        return str(response.id)


def _bind_profile_home(adapter, home):
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: home,
    )


def test_home_view_shows_message_edits_row_with_beta_badge(tmp_path):
    """The settings table gains Message edits, badged beta, off by default."""
    adapter = FakeAdapter()
    _bind_profile_home(adapter, tmp_path)
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    state = ui._new_state(source, tmp_path, message_id=55)
    state["user_name"] = source.user_name
    text, _markup = ui._home_view(state)
    assert ('<td><b>Message edits</b> <tg-button type="callback_data" style="primary" '
            'data="badge:beta">beta</tg-button></td><td>Off</td>') in text
    assert "message-edits-toggle" in text


@pytest.mark.asyncio
async def test_message_edits_toggle_flips_profile_config(tmp_path):
    """The owner's toggle persists platforms.telegram.message_edits and re-renders it."""
    from hermes_cli.config import read_user_config_raw

    adapter = FakeAdapter()
    _bind_profile_home(adapter, tmp_path)
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    state = ui._new_state(source, tmp_path, message_id=55)
    state["user_name"] = source.user_name

    event = CallbackEvent(55)
    assert await ui.handle_callback(event, source, f"ps:{state['id']}:message-edits-toggle")
    assert read_user_config_raw(tmp_path / "config.yaml")["platforms"]["telegram"]["message_edits"] is True
    assert getattr(adapter, "_message_edits_runtime", None) is True
    text, _markup = ui._home_view(state)
    assert ('<td><b>Message edits</b> <tg-button type="callback_data" style="primary" '
            'data="badge:beta">beta</tg-button></td><td>On</td>') in text


# ── The edit ack: only for a message Hermes already answered ─────────────


def _group_edit_setup(monkeypatch, tmp_path):
    """Adapter wired to drive a group 'Emilia' through _process_inbound_event:
    owner sender, bot triggered, the turn stubbed, sends captured."""
    from plugins.platforms.telegram import button_demo

    adapter = _adapter(edits=True)
    adapter._bot_id = 900
    adapter._bot_username = "AgainEmiliaBot"
    source = SimpleNamespace(chat_id="-1002397157264", user_id="42", user_name="owner",
                             chat_type="group", thread_id=None, is_bot=False,
                             profile_route_rejected=False)
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(adapter, "_is_owner_user", lambda user_id, source=None: True)
    monkeypatch.setattr(adapter, "_settings_home", lambda _source: tmp_path)
    monkeypatch.setattr(adapter, "_multiuser_home", lambda _source: None)
    monkeypatch.setattr(adapter, "_observe_telegram_message", lambda *_args: None)
    monkeypatch.setattr(adapter, "_group_triggered", AsyncMock(return_value=True))
    monkeypatch.setattr(button_demo, "handle_reply_keyboard_text", AsyncMock(return_value=False))
    monkeypatch.setattr(button_demo, "clear_reply_keyboard_for_command", AsyncMock(return_value=None))
    sent = []
    monkeypatch.setattr(adapter, "send", AsyncMock(side_effect=lambda *args, **kwargs: sent.append((args, kwargs))))
    monkeypatch.setattr(adapter, "send_reaction", AsyncMock())
    handled = []
    monkeypatch.setattr(adapter, "handle_message", AsyncMock(side_effect=lambda event: handled.append(event)))
    return adapter, sent, handled


def _edit_event(message_id=400):
    """A MessageEdited event for a message, as _on_message_edited marks it."""
    message = SimpleNamespace(message="Emilia", id=message_id)
    event = SimpleNamespace(message=message)
    event._hermes_message_edit = True
    return event, message


@pytest.mark.asyncio
async def test_edit_of_unanswered_message_sends_no_ack_but_still_runs_the_turn(monkeypatch, tmp_path):
    """Live: a fresh 'Emilia' got 'I saw your edit.' though Hermes never
    answered it. The ack fires only for an edit of a message Hermes already
    answered; the edit turn itself still runs."""
    adapter, sent, handled = _group_edit_setup(monkeypatch, tmp_path)
    event, message = _edit_event()

    await adapter._process_inbound_event(event, [message])

    assert handled, "the edit turn never ran"
    assert handled[0].edit_revision is not None
    assert not any(args[1] == ACK_TEXT for args, _kwargs in sent), sent


@pytest.mark.asyncio
async def test_edit_of_answered_message_still_acks(monkeypatch, tmp_path):
    """The intended ack: Hermes answered the message, the user edits it, the
    ack confirms the edit and the turn runs."""
    adapter, sent, handled = _group_edit_setup(monkeypatch, tmp_path)
    record_reply(adapter, "-1002397157264", "400", "900")
    event, message = _edit_event()

    await adapter._process_inbound_event(event, [message])

    assert handled, "the edit turn never ran"
    assert any(args[1] == ACK_TEXT and kwargs.get("reply_to") == 400
               for args, kwargs in sent), sent


# ── A refused message never arms the edit tracker ─────────────────────────


def _ledger_adapter(tmp_path, monkeypatch):
    """Real TelethonAdapter whose MTProto client records requests, over a
    temp delivery ledger (same shape as the footer-badges repro)."""
    import os
    from unittest.mock import MagicMock

    from gateway import delivery_ledger as dl
    from gateway.platforms.base import SendResult
    from persistence_assembly import profile_scope

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_scope", lambda: profile_scope(home=home))
    monkeypatch.setattr(dl, "_owner_stamp", lambda: (os.getpid(), 202))
    monkeypatch.setattr(dl, "ledger_enabled", lambda config=None: True)

    adapter = TelethonAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))

    class Client:
        def __init__(self):
            self.requests = []

        async def __call__(self, request):
            self.requests.append(request)
            return SimpleNamespace(id=777)

    adapter._client = Client()
    runner = MagicMock()
    runner._delivery_adapter_for = MagicMock(return_value=adapter)
    runner._schedule_flood_redelivery = MagicMock(return_value=187.0)
    adapter.gateway_runner = runner
    adapter.edit_message = AsyncMock(
        return_value=SendResult(success=False, error="message to edit not found"))
    return adapter


def _dm_source():
    return SimpleNamespace(platform=Platform.TELEGRAM, chat_id="123", thread_id=None,
                           chat_type="dm", parent_chat_id=None, user_id="6802929470")


@pytest.mark.asyncio
async def test_refusal_notice_send_records_no_reply_for_the_refused_message(tmp_path, monkeypatch):
    """A refused message was never answered, so its refusal card must not arm
    the edit tracker: a later edit of it must not ack. A normal turn reply
    still records, so the intended ack keeps working."""
    adapter = _ledger_adapter(tmp_path, monkeypatch)
    refused = MessageEvent(text="Emilia", source=_dm_source(), message_id="101")
    refused._hermes_admission_refused = True
    await adapter._send_final_text(
        refused, "agent:main:telegram:dm:123",
        "⚠️ Hermes refused this message (invalid_params), so it was not saved or answered. "
        "Ask the operator to check gateway.log.",
        {}, False, 0, lambda _r: None)
    assert previous_reply_id_for(adapter, "123", "101") is None

    answered = MessageEvent(text="Emilia", source=_dm_source(), message_id="102")
    await adapter._send_final_text(answered, "agent:main:telegram:dm:123", "Hey!", {}, False, 0, lambda _r: None)
    assert previous_reply_id_for(adapter, "123", "102") == "777"
