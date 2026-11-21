"""``hm:sessions:page:`` callback gate: the Next/Previous pager on a /sessions
listing must reach its handler for an admitted user in a group, not die in
the multiuser policy gate that demands a DM plus the owner.

Same gate bug as the ``hm:memories:page:`` pager (fixed on its own branch):
tapping Next on the group /sessions card answers "This control is not
authorized for your account or chat scope." for the owner himself, because
the ``hm:`` catch-all gate requires ``private``. The pager re-renders a
listing already posted in the chat, bound to its short-lived token, so no DM
or owner gate applies — admission is still re-checked by the dispatch-level
``_authorized``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def _multiuser_adapter(monkeypatch, tmp_path, *, owner_id="7"):
    """An adapter with multiuser mode on, so the callback gate applies."""
    import gateway.mongo_multiuser as multiuser

    monkeypatch.setattr(multiuser, "enabled", lambda _home: True)
    monkeypatch.setattr(multiuser, "owner_user_id", lambda _home: owner_id)
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: tmp_path,
    )
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
async def test_sessions_page_callback_reaches_handler_for_owner_in_group(monkeypatch, tmp_path):
    adapter = _multiuser_adapter(monkeypatch, tmp_path)
    source = SimpleNamespace(chat_id="1002397157264", user_id="7", chat_type="group")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    handler = AsyncMock()
    monkeypatch.setattr(adapter, "_handle_sessions_page_callback", handler)
    event, answered = _callback_event(b"hm:sessions:page:a1b2c3d4:2")

    await adapter._on_callback_query(event)

    assert answered == []
    handler.assert_awaited_once()
    assert handler.await_args.args[2] == "hm:sessions:page:a1b2c3d4:2"


@pytest.mark.asyncio
async def test_sessions_page_callback_still_denies_unadmitted_user(monkeypatch, tmp_path):
    adapter = _multiuser_adapter(monkeypatch, tmp_path)
    source = SimpleNamespace(chat_id="1002397157264", user_id="999", chat_type="group")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: False)
    handler = AsyncMock()
    monkeypatch.setattr(adapter, "_handle_sessions_page_callback", handler)
    event, answered = _callback_event(b"hm:sessions:page:a1b2c3d4:2")

    await adapter._on_callback_query(event)

    assert answered == [(("This control is not authorized for your account or chat scope.",),
                         {"alert": True})]
    handler.assert_not_awaited()
