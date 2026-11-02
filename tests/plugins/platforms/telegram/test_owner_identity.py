"""Owner identity never follows allowlist roles or leaks across routed profiles."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.mongo_multiuser import store_for_home
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def source(uid, home):
    return SimpleNamespace(user_id=uid, chat_id=uid, chat_type="dm", thread_id=None,
                           is_bot=False, home=home)


def test_owner_identity_is_profile_scoped_and_allowlist_never_promotes(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    for home, owner in ((a, "100"), (b, "300")):
        home.mkdir()
        (home / ".env").write_text(f"OWNER_TELEGRAM_ID={owner}\nTELEGRAM_ALLOWED_USERS=200\n")
    monkeypatch.setenv("HERMES_HOME", str(a))
    adapter = TelethonAdapter(PlatformConfig())
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda src: src.home)
    for home, owner in ((a, "100"), (b, "300"), (a, "100")):
        allowed = source("200", home)
        store_for_home(home).start_user("200")
        assert adapter._authorized(allowed)
        assert adapter._is_explicitly_allowed(allowed)
        assert not adapter._is_owner_user("200", allowed)
        assert adapter._is_owner_user(owner, source(owner, home))
        other = "300" if owner == "100" else "100"
        assert not adapter._is_owner_user(other, source(other, home))
        assert not adapter._diagnostics_ui()._allowed(allowed, home)
        assert adapter._diagnostics_ui()._allowed(source(owner, home), home)
    (a / ".env").write_text("TELEGRAM_ALLOWED_USERS=100,200\nGATEWAY_ALLOWED_USERS=100\n")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "100,200")
    monkeypatch.setenv("GATEWAY_ALLOWED_USERS", "100")
    assert not adapter._is_owner_user("100")
    assert not adapter._is_owner_user("200")
    for invalid in ("", "0", "-100", "0100", "100,200", "@owner"):
        assert not adapter._is_owner_user(invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["logs", "hf"])
async def test_diagnostics_without_owner_policy_deny_allowlisted_identity(tmp_path, monkeypatch, command):
    (tmp_path / ".env").write_text("TELEGRAM_ALLOWED_USERS=200\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "200")
    adapter = TelethonAdapter(PlatformConfig())
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda src: tmp_path)
    adapter._authorized = lambda src: True
    adapter.send = AsyncMock()
    adapter._client = AsyncMock()
    await adapter._diagnostics_ui().open(source("200", tmp_path), command)
    adapter._client.assert_not_awaited()
    assert "only to the owner" in adapter.send.await_args.args[1]


@pytest.mark.asyncio
async def test_allowall_off_does_not_disable_authorized_name_invocation(tmp_path, monkeypatch):
    from plugins.platforms.telegram.telegram_chat_settings import update_chat_settings
    (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=200\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = TelethonAdapter(PlatformConfig(extra={"require_mention": True}))
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda src: tmp_path)
    adapter._bot_username = "AssistantBot"
    adapter._bot_name = "Assistant"
    store_for_home(tmp_path).start_user("200")
    update_chat_settings(tmp_path, "group", "-100", {"allow_all": False, "trigger_mode": "trigger"})
    event = SimpleNamespace(message=SimpleNamespace(message="Assistant hello", reply_to_msg_id=None))
    for uid, expected in (("100", True), ("200", True), ("300", False)):
        src = source(uid, tmp_path)
        src.chat_type, src.chat_id = "group", "-100"
        assert await adapter._group_triggered(event, src) is expected
    update_chat_settings(tmp_path, "group", "-100", {"allow_all": True})
    guest = source("300", tmp_path)
    guest.chat_type, guest.chat_id = "group", "-100"
    assert await adapter._group_triggered(event, guest)
    assert adapter._authorized(guest)
    guest.chat_id = "-200"
    assert not adapter._authorized(guest)
    guest.chat_type, guest.chat_id = "dm", "300"
    assert not adapter._authorized(guest)


@pytest.mark.asyncio
async def test_native_allowlist_pages_and_disallow_recheck_real_owner(tmp_path, monkeypatch):
    from agent.mongo_tenants import Principal
    from telethon import functions, types
    (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=200\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = TelethonAdapter(PlatformConfig())
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda src: tmp_path)
    adapter._bot_username = "AssistantBot"
    adapter._source = AsyncMock(side_effect=lambda event: event.source)
    adapter._cache_inbound_peer = AsyncMock()
    adapter._client = AsyncMock(return_value=SimpleNamespace(id=900))
    adapter._peer_for = lambda chat_id: types.InputPeerSelf()
    adapter.send = AsyncMock()
    store = store_for_home(tmp_path)
    for uid in range(201, 225):
        store.allow_user(Principal("100"), str(uid), True)
    owner = source("100", tmp_path)
    async def command(text, src=owner):
        event = SimpleNamespace(source=src, message=SimpleNamespace(message=text, id=50))
        await adapter._on_new_message(event)
    await command("/allowlist")
    request = adapter._client.await_args.args[0]
    assert isinstance(request, functions.messages.SendMessageRequest)
    assert isinstance(request.rich_message, types.InputRichMessageHTML)
    assert "Page 1 of 2" in request.rich_message.html
    data = request.reply_markup.rows[0].buttons[0].type.data
    callback = SimpleNamespace(source=owner, data=data, message_id=900, answer=AsyncMock())
    await adapter._on_callback_query(callback)
    edited = adapter._client.await_args.args[0]
    assert isinstance(edited, functions.messages.EditMessageRequest)
    assert edited.id == 900 and "Page 2 of 2" in edited.rich_message.html
    callback.source = source("200", tmp_path)
    before = adapter._client.await_count
    await adapter._on_callback_query(callback)
    assert adapter._client.await_count == before
    assert callback.answer.await_args.kwargs["alert"]
    await command("/disallow 201", source("200", tmp_path))
    assert store.get_access("201")["allowed"]
    await command("/disallow 201")
    assert not store.get_access("201")["allowed"]
    assert store.get_access("202")["allowed"]
    assert store_for_home(tmp_path).get_access("201")["allowed"] is False
    assert not adapter._authorized(source("201", tmp_path))


@pytest.mark.asyncio
async def test_native_group_model_settings_button_alerts_dm_without_new_card(tmp_path, monkeypatch):
    from telethon import types
    (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=200\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = TelethonAdapter(PlatformConfig())
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda src: tmp_path,
        _handle_msettings_command=AsyncMock(),
    )
    adapter._source = AsyncMock(side_effect=lambda event: event.source)
    adapter._client = AsyncMock(return_value=SimpleNamespace(id=900))
    adapter._peer_for = lambda chat_id: types.InputPeerSelf()
    group = source("100", tmp_path)
    group.chat_type, group.chat_id = "group", "-100"
    ui = adapter._model_settings_ui()
    await ui.open_dashboard(group, {"current_model": "m", "current_provider": "p"}, [],
                            "m", "p", "group-session", tmp_path)
    state = next(iter(ui._states.values()))
    callback = SimpleNamespace(source=group, data=f"ms:{state['id']}:settings".encode(),
                               message_id=900, answer=AsyncMock())
    await adapter._on_callback_query(callback)
    assert "Telegram DM" in callback.answer.await_args.args[0]
    assert callback.answer.await_args.kwargs["alert"]
    assert adapter._client.await_count == 1
    adapter.gateway_runner._handle_msettings_command.assert_not_awaited()
    assert state["view"] == "dashboard"
