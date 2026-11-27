"""Guest history control: the "Save guest messages" setting and its menu.

Invariant tests, written red-first. Saving defaults to ON (today's
behaviour); the owner flips it off in /guestbot, and the toggle persists to
the profile's config.yaml. The menu shows the toggle and a Clear button.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.guestbot import (
    guest_save_history,
    persist_guest_save_history_setting,
)
from plugins.platforms.telegram.rich_ui import guestbot_view
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def _adapter(tmp_path, *, extra=None):
    adapter = TelethonAdapter(PlatformConfig(extra=extra or {}))
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: tmp_path,
    )
    return adapter


def _source():
    return SimpleNamespace(chat_id="999", user_id="999", chat_type="dm")


# --- the setting ---------------------------------------------------------------

def test_guest_save_history_defaults_to_on(tmp_path):
    assert guest_save_history(_adapter(tmp_path)) is True
    assert guest_save_history(_adapter(tmp_path, extra={"guest_save_history": True})) is True


def test_guest_save_history_off_after_persist(tmp_path):
    adapter = _adapter(tmp_path)
    persist_guest_save_history_setting(adapter, _source(), False)
    assert guest_save_history(adapter) is False
    assert "guest_save_history: false" in (tmp_path / "config.yaml").read_text(encoding="utf-8")


def test_guest_save_history_back_on_after_persist(tmp_path):
    adapter = _adapter(tmp_path)
    persist_guest_save_history_setting(adapter, _source(), False)
    persist_guest_save_history_setting(adapter, _source(), True)
    assert guest_save_history(adapter) is True
    assert "guest_save_history: true" in (tmp_path / "config.yaml").read_text(encoding="utf-8")


# --- the menu ------------------------------------------------------------------

def test_guestbot_view_shows_save_toggle_and_clear():
    text, markup = guestbot_view(True)
    assert "Save guest messages" in text
    assert 'data="hm:guestbot:save:off"' in text and "Turn off" in text
    assert "Clear guest history" in text
    assert 'data="hm:guestbot:clear"' in text
    keyboard_labels = [button.text for row in markup.rows for button in row.buttons]
    assert keyboard_labels == ["Back to help"]

    text, _markup = guestbot_view(True, save_history=False)
    assert "Save guest messages" in text
    assert 'data="hm:guestbot:save:on"' in text and "Turn on" in text
    assert "not saved" in text.lower()


def test_guestbot_view_keeps_the_existing_toggle():
    # The on/off toggle is unchanged by the new rows.
    text, _markup = guestbot_view(False)
    assert 'data="hm:guestbot:on"' in text and "Turn on" in text
    text, _markup = guestbot_view(True, note="GuestBot is enabled.")
    assert 'data="hm:guestbot:off"' in text and "Turn off" in text


# --- the save callback ---------------------------------------------------------

class _Client:
    """Fake Telethon client recording requests."""

    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace(id=17)


def _multiuser_adapter(monkeypatch, tmp_path, *, owner_id="7"):
    import gateway.mongo_multiuser as multiuser

    monkeypatch.setattr(multiuser, "enabled", lambda _home: True)
    monkeypatch.setattr(multiuser, "owner_user_id", lambda _home: owner_id)
    adapter = _adapter(tmp_path)
    return adapter


def _callback_event(data: bytes, message_id: int = 17):
    answered = []

    class Event:
        async def answer(self, *args, **kwargs):
            answered.append((args, kwargs))

    event = Event()
    event.data = data
    event.message_id = message_id
    return event, answered


@pytest.mark.asyncio
async def test_save_off_callback_persists_and_rerenders(monkeypatch, tmp_path):
    adapter = _multiuser_adapter(monkeypatch, tmp_path)
    source = SimpleNamespace(chat_id="7", user_id="7", chat_type="dm")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    adapter._client = _Client()
    event, answered = _callback_event(b"hm:guestbot:save:off")

    await adapter._on_callback_query(event)

    assert guest_save_history(adapter) is False
    assert "guest_save_history: false" in (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert answered  # the tap is answered, never left hanging
    edits = [r for r in adapter._client.requests if type(r).__name__ == "EditMessageRequest"]
    assert edits  # the menu is re-rendered in place with the new state


@pytest.mark.asyncio
async def test_save_on_callback_restores_saving(monkeypatch, tmp_path):
    adapter = _multiuser_adapter(monkeypatch, tmp_path)
    persist_guest_save_history_setting(adapter, _source(), False)
    source = SimpleNamespace(chat_id="7", user_id="7", chat_type="dm")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    adapter._client = _Client()
    event, answered = _callback_event(b"hm:guestbot:save:on")

    await adapter._on_callback_query(event)

    assert guest_save_history(adapter) is True
    assert answered
