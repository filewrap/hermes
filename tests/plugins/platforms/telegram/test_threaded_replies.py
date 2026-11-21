"""Threaded replies (beta): turn responses chain to each other's messages.

When the beta toggle is on, the second response message in a turn replies to
the first response message (not to the user's message), the third replies to
the second, and so on. When it is off, every response replies to the user's
message, as before. A send that fails (or returns no id) breaks the chain:
the next response falls back to the user's message.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
from plugins.platforms.telegram.threaded_replies import threaded_replies_enabled
from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI


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


def _adapter(*, threaded: bool = False) -> TelethonAdapter:
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = _Client()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(42, 0))
    if threaded:
        adapter._threaded_replies_runtime = True
    return adapter


def _reply_ids(adapter: TelethonAdapter) -> list[int]:
    return [request.reply_to.reply_to_msg_id for request in adapter._client.requests]


@pytest.mark.asyncio
async def test_threaded_on_chains_second_and_third_message_to_previous():
    """ON: msg 2 replies to msg 1 (id 900), msg 3 replies to msg 2 (id 901)."""
    adapter = _adapter(threaded=True)
    for text in ("Hey nice to meet you.", "I don't remember your name.",
                 "can you please say that again?"):
        result = await adapter.send(CHAT, text, reply_to=ANCHOR, metadata=dict(FLAG))
        assert result.success
    assert _reply_ids(adapter) == [400, 900, 901]


@pytest.mark.asyncio
async def test_threaded_off_all_messages_quote_the_user_message():
    """OFF: every response replies to the user's message, as today."""
    adapter = _adapter(threaded=False)
    for text in ("one", "two", "three"):
        result = await adapter.send(CHAT, text, reply_to=ANCHOR, metadata=dict(FLAG))
        assert result.success
    assert _reply_ids(adapter) == [400, 400, 400]


@pytest.mark.asyncio
async def test_threaded_flag_alone_does_not_chain_without_the_setting():
    """The flag marks response sends; without the toggle on, nothing chains."""
    adapter = _adapter(threaded=False)
    result = await adapter.send(CHAT, "one", reply_to=ANCHOR, metadata=dict(FLAG))
    assert result.success
    assert _reply_ids(adapter) == [400]


def test_threaded_replies_default_is_off():
    """A fresh adapter (no config key, no runtime flag) has threading off."""
    assert threaded_replies_enabled(TelethonAdapter(PlatformConfig())) is False


def test_threaded_replies_reads_profile_config_extra():
    """The persisted profile key turns threading on without a live toggle."""
    adapter = TelethonAdapter(PlatformConfig(extra={"threaded_replies": True}))
    assert threaded_replies_enabled(adapter) is True


@pytest.mark.asyncio
async def test_threaded_failed_send_falls_back_to_user_message():
    """A failed send breaks the chain: the next response quotes the user message."""
    adapter = _adapter(threaded=True)
    adapter._client = _Client(fail_on=(2,))
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(42, 0))
    first = await adapter.send(CHAT, "one", reply_to=ANCHOR, metadata=dict(FLAG))
    assert first.success
    second = await adapter.send(CHAT, "two", reply_to=ANCHOR, metadata=dict(FLAG))
    assert not second.success
    third = await adapter.send(CHAT, "three", reply_to=ANCHOR, metadata=dict(FLAG))
    assert third.success
    assert _reply_ids(adapter) == [400, 900, 400]


@pytest.mark.asyncio
async def test_threaded_unflagged_sends_do_not_chain_and_do_not_break_the_chain():
    """Working/status sends carry no flag: they quote the anchor and leave
    the response chain pointing at the last response message."""
    adapter = _adapter(threaded=True)
    await adapter.send(CHAT, "response one", reply_to=ANCHOR, metadata=dict(FLAG))
    await adapter.send(CHAT, "status update", reply_to=ANCHOR, metadata={})
    await adapter.send(CHAT, "response two", reply_to=ANCHOR, metadata=dict(FLAG))
    assert _reply_ids(adapter) == [400, 400, 900]


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


def test_home_view_shows_threaded_replies_row_with_beta_badge(tmp_path):
    """The settings table gains Threaded replies, badged beta, off by default."""
    adapter = FakeAdapter()
    _bind_profile_home(adapter, tmp_path)
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    state = ui._new_state(source, tmp_path, message_id=55)
    state["user_name"] = source.user_name
    text, _markup = ui._home_view(state)
    assert ('<td><b>Threaded replies</b> <tg-button type="callback_data" style="primary" '
            'data="badge:beta">beta</tg-button></td><td>Off</td>') in text
    assert "threaded-replies-toggle" in text


@pytest.mark.asyncio
async def test_threaded_replies_toggle_flips_profile_config(tmp_path):
    """The owner's toggle persists platforms.telegram.threaded_replies and re-renders it."""
    from hermes_cli.config import read_user_config_raw

    adapter = FakeAdapter()
    _bind_profile_home(adapter, tmp_path)
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    state = ui._new_state(source, tmp_path, message_id=55)
    state["user_name"] = source.user_name

    event = CallbackEvent(55)
    assert await ui.handle_callback(event, source, f"ps:{state['id']}:threaded-replies-toggle")
    assert read_user_config_raw(tmp_path / "config.yaml")["platforms"]["telegram"]["threaded_replies"] is True
    assert getattr(adapter, "_threaded_replies_runtime", None) is True
    text, _markup = ui._home_view(state)
    assert ('<td><b>Threaded replies</b> <tg-button type="callback_data" style="primary" '
            'data="badge:beta">beta</tg-button></td><td>On</td>') in text
