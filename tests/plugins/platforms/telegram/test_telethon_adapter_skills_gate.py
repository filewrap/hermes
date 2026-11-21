"""Skills-menu callbacks are usable in groups for any admitted user."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def _skills_gate_adapter(monkeypatch, tmp_path, *, owner_id="7"):
    """An adapter with multiuser mode on, so the callback gate applies."""
    import gateway.mongo_multiuser as multiuser

    monkeypatch.setattr(multiuser, "enabled", lambda _home: True)
    monkeypatch.setattr(multiuser, "owner_user_id", lambda _home: owner_id)
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: tmp_path,
    )
    return adapter


def _skills_callback_event(data: bytes, message_id: int = 17):
    answered = []

    class Event:
        async def answer(self, *args, **kwargs):
            answered.append((args, kwargs))

    event = Event()
    event.data = data
    event.message_id = message_id
    return event, answered


@pytest.mark.asyncio
async def test_skills_callback_allowed_in_group_for_admitted_non_owner(monkeypatch, tmp_path):
    adapter = _skills_gate_adapter(monkeypatch, tmp_path)
    source = SimpleNamespace(chat_id="-100123", user_id="999", user_name="Member",
                             chat_type="group", thread_id=None, is_bot=False)
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(adapter, "_peer_for", lambda chat: chat)
    adapter._client = SimpleNamespace()
    edits = []

    async def _record(request):
        edits.append(request)
        return SimpleNamespace(id=17)

    monkeypatch.setattr(adapter, "_mtproto_rich_request", _record)
    ui = adapter._skills_ui()
    state = ui._new_state(source, tmp_path, message_id=17)
    event, answered = _skills_callback_event(f"sk:{state['id']}:home".encode(), message_id=17)

    await adapter._on_callback_query(event)

    assert not any("not authorized" in str(args) for args, _ in answered)
    assert len(edits) == 1
