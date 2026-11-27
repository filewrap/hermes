"""``hm:resume:`` / ``hm:rename:`` / ``hm:reasoning:`` callback gate: the
Resume, Rename and reasoning-card buttons on a /sessions (or /reasoning)
listing must reach their handlers for an admitted user in a group, not die
in the multiuser policy gate that demands a DM plus the owner.

Same gate bug as the ``hm:memories:page:`` / ``hm:sessions:page:`` pagers:
tapping Resume on the group /sessions card answers "This control is not
authorized for your account or chat scope." for the owner himself, because
the ``hm:`` catch-all gate requires ``private``. All three re-enter the
normal command path behind the session IDOR guard (resume re-dispatches
/resume, rename arms the rename prompt, reasoning re-dispatches /reasoning),
so a forged callback is no stronger than typing the command — admission is
still re-checked, and the handler decides. Personal settings stay DM-only:
``hm:user:`` renders the tapper's private settings into the chat.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

_SESSION_ID = "20261120_155247_94197c0d"


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


_DENIED = [(("This control is not authorized for your account or chat scope.",),
            {"alert": True})]


@pytest.mark.asyncio
async def test_resume_callback_reaches_handler_for_owner_in_group(monkeypatch, tmp_path):
    adapter = _multiuser_adapter(monkeypatch, tmp_path)
    source = SimpleNamespace(chat_id="1002397157264", user_id="7", chat_type="group")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    handler = AsyncMock()
    monkeypatch.setattr(adapter, "_handle_resume_callback", handler)
    event, answered = _callback_event(f"hm:resume:{_SESSION_ID}".encode())

    await adapter._on_callback_query(event)

    assert answered == []
    handler.assert_awaited_once()
    assert handler.await_args.args[2] == f"hm:resume:{_SESSION_ID}"


@pytest.mark.asyncio
async def test_rename_callback_reaches_handler_for_owner_in_group(monkeypatch, tmp_path):
    adapter = _multiuser_adapter(monkeypatch, tmp_path)
    source = SimpleNamespace(chat_id="1002397157264", user_id="7", chat_type="group")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    handler = AsyncMock()
    monkeypatch.setattr(adapter, "_handle_rename_callback", handler)
    event, answered = _callback_event(f"hm:rename:{_SESSION_ID}".encode())

    await adapter._on_callback_query(event)

    assert answered == []
    handler.assert_awaited_once()
    assert handler.await_args.args[2] == f"hm:rename:{_SESSION_ID}"


@pytest.mark.asyncio
async def test_reasoning_callback_reaches_handler_for_owner_in_group(monkeypatch, tmp_path):
    adapter = _multiuser_adapter(monkeypatch, tmp_path)
    source = SimpleNamespace(chat_id="1002397157264", user_id="7", chat_type="group")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    handler = AsyncMock()
    monkeypatch.setattr(adapter, "_handle_reasoning_callback", handler)
    event, answered = _callback_event(b"hm:reasoning:toggle-display")

    await adapter._on_callback_query(event)

    assert answered == []
    handler.assert_awaited_once()
    assert handler.await_args.args[2] == "hm:reasoning:toggle-display"


@pytest.mark.asyncio
async def test_resume_callback_reaches_handler_for_admitted_nonowner_in_group(monkeypatch, tmp_path):
    adapter = _multiuser_adapter(monkeypatch, tmp_path)
    source = SimpleNamespace(chat_id="1002397157264", user_id="999", chat_type="group")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    handler = AsyncMock()
    monkeypatch.setattr(adapter, "_handle_resume_callback", handler)
    event, answered = _callback_event(f"hm:resume:{_SESSION_ID}".encode())

    await adapter._on_callback_query(event)

    assert answered == []
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_callback_still_denies_unadmitted_user(monkeypatch, tmp_path):
    adapter = _multiuser_adapter(monkeypatch, tmp_path)
    source = SimpleNamespace(chat_id="1002397157264", user_id="999", chat_type="group")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: False)
    handler = AsyncMock()
    monkeypatch.setattr(adapter, "_handle_resume_callback", handler)
    event, answered = _callback_event(f"hm:resume:{_SESSION_ID}".encode())

    await adapter._on_callback_query(event)

    assert answered == _DENIED
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_personal_settings_still_denied_in_group(monkeypatch, tmp_path):
    adapter = _multiuser_adapter(monkeypatch, tmp_path)
    source = SimpleNamespace(chat_id="1002397157264", user_id="7", chat_type="group")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    event, answered = _callback_event(b"hm:user:open")

    await adapter._on_callback_query(event)

    assert answered == _DENIED
