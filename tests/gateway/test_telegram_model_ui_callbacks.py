from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.model_ui import dashboard_payload
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.slash_commands_model import GatewayModelCommandsMixin
from plugins.platforms.telegram.model_settings_ui import TelegramModelSettingsUI


class _Adapter:
    def __init__(self):
        self._client = object()
        self.gateway_runner = None
        self.ui = TelegramModelSettingsUI(self)
        self.requests = []

    def _is_owner_user(self, user_id, source=None):
        return (getattr(source, "profile", None) == "alpha"
                and str(user_id or "") == "42")

    def _peer_for(self, chat_id):
        return ("peer", str(chat_id))

    async def _mtproto_rich_request(self, request):
        self.requests.append(request)
        return SimpleNamespace(id=700 + len(self.requests))

    @staticmethod
    def _sent_message_id(result):
        return str(result.id)

    def _model_settings_ui(self):
        return self.ui

    async def send_model_dashboard(self, **kwargs):
        return await self.ui.open_dashboard(**kwargs)

    async def send_model_settings(self, **kwargs):
        return await self.ui.open_settings(**kwargs)


class _Runner(GatewayModelCommandsMixin):
    def __init__(self, adapter, profile_home):
        from gateway.session_state import SessionState

        self.adapter = adapter
        self.config = SimpleNamespace(multiplex_profiles=False)
        self.profile_home = profile_home
        self.sessions = {"session-A": SessionState()}
        self.inventory_calls = 0
        self._session_model_overrides = {}
        self._session_reasoning_overrides = {}
        adapter.gateway_runner = self

    def _delivery_adapter_for(self, _source):
        return self.adapter

    def _reply_anchor_for_event(self, _event):
        return None

    def _normalize_source_for_session_key(self, source):
        return source

    def _session_key_for_source(self, _source):
        return "session-A"

    def _session_state(self, session_key):
        return self.sessions[session_key]

    def _peek_session_state(self, session_key):
        return self.sessions.get(session_key)

    def _model_settings_owner_error(self, _source, _profile_home):
        return None

    def _resolve_profile_home_for_source(self, _source):
        return self.profile_home

    def _evict_cached_agent(self, _session_key):
        pass

    def _set_session_reasoning_override(self, session_key, value):
        if value is None:
            self._session_reasoning_overrides.pop(session_key, None)
        else:
            self._session_reasoning_overrides[session_key] = value

    def _apply_reasoning_selection(self, session_key, _platform, effort, persist_global=False):
        self._session_reasoning_overrides[session_key] = effort
        return f"Reasoning effort set to {effort}."

    async def _provider_inventory_snapshot(self, _source):
        self.inventory_calls += 1
        providers = [{"slug": "openrouter", "name": "OpenRouter", "configured": True,
                      "total_models": 4, "reasoning_supported": True,
                      "reasoning_efforts": ["low", "high"]}]
        return providers, "model-x", "openrouter", "session-A", self.profile_home


def _source(thread=None):
    return SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="42",
                         chat_type="dm", profile="alpha", thread_id=thread)


def _callback(message_id, query_id, data):
    class Callback:
        def __init__(self):
            self.message_id = message_id
            self.query_id = query_id
            self.data = data
            self.answers = []

        async def answer(self, text="", alert=False):
            self.answers.append((text, alert))

    return Callback()


@pytest.mark.asyncio
async def test_settings_navigation_and_back_reuse_cached_provider_inventory(tmp_path):
    adapter = _Adapter()
    runner = _Runner(adapter, Path(tmp_path))
    source = _source()
    providers, model, provider, session_key, home = await runner._provider_inventory_snapshot(source)
    dashboard = dashboard_payload(current_model=model, current_provider=provider,
                                  providers=providers, settings={}, reasoning_effort="")
    await adapter.ui.open_dashboard(source, dashboard, providers, model, provider,
                                   session_key, home)
    state = next(iter(adapter.ui._states.values()))
    original_id = state["message_id"]
    assert runner.inventory_calls == 1

    await adapter.ui.handle_callback(
        _callback(original_id, 100, f"ms:{state['id']}:settings"), source,
        f"ms:{state['id']}:settings",
    )
    assert state["view"] == "settings"
    assert runner.inventory_calls == 1
    assert adapter.requests[-1].id == int(original_id)

    await adapter.ui.handle_callback(
        _callback(original_id, 101, f"ms:{state['id']}:page:output"), source,
        f"ms:{state['id']}:page:output",
    )
    assert state["page"] == "output"
    assert runner.inventory_calls == 1

    await adapter.ui.handle_callback(
        _callback(original_id, 102, f"ms:{state['id']}:inc:max_tokens"), source,
        f"ms:{state['id']}:inc:max_tokens",
    )
    assert runner.sessions[session_key].conversation.model_settings["max_tokens"] == 2
    assert runner.inventory_calls == 1
    assert adapter.requests[-1].id == int(original_id)

    await adapter.ui.handle_callback(
        _callback(original_id, 103, f"ms:{state['id']}:back"), source,
        f"ms:{state['id']}:back",
    )
    assert state["view"] == "settings"
    assert state["page"] == "sampling"
    assert runner.inventory_calls == 1
    assert adapter.requests[-1].id == int(original_id)

    await adapter.ui.handle_callback(
        _callback(original_id, 104, f"ms:{state['id']}:back"), source,
        f"ms:{state['id']}:back",
    )
    assert state["view"] == "dashboard"
    assert runner.inventory_calls == 1
    assert adapter.requests[-1].id == int(original_id)
    assert len(adapter.ui._states) == 1


def test_internal_callback_context_is_not_read_from_untrusted_event_metadata(tmp_path):
    adapter = _Adapter()
    _Runner(adapter, Path(tmp_path))
    source = _source()
    event = MessageEvent(text="/msettings page 1", message_type=MessageType.COMMAND,
                         source=source,
                         metadata={"_telegram_model_ui_state_id": 9,
                                   "_telegram_model_ui_token": object()})
    assert adapter.ui.callback_context(event) is None


@pytest.mark.asyncio
async def test_model_settings_handler_checks_real_owner_and_dm_before_mutation(tmp_path):
    (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=42\nTELEGRAM_ALLOWED_USERS=99\n")
    adapter = _Adapter()
    runner = _Runner(adapter, tmp_path)
    runner._model_settings_owner_error = GatewayModelCommandsMixin._model_settings_owner_error.__get__(runner)
    for uid, kind in (("99", "dm"), ("42", "group")):
        src = SessionSource(platform=Platform.TELEGRAM, chat_id=uid if kind == "dm" else "-100",
                            user_id=uid, chat_type=kind, profile="alpha")
        event = MessageEvent(text="/msettings set temperature 0.5", message_type=MessageType.COMMAND,
                             source=src)
        denial = await runner._handle_msettings_command(event)
        assert "owner" in denial.lower()
        assert runner.sessions["session-A"].conversation.model_settings == {}
        assert adapter.requests == []
    event = MessageEvent(text="/msettings set temperature 0.5", message_type=MessageType.COMMAND,
                         source=_source())
    assert await runner._handle_msettings_command(event) is None
    assert runner.sessions["session-A"].conversation.model_settings == {"temperature": 0.5}
    assert adapter.requests[-1].rich_message.html


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["config", "override"])
async def test_cached_controls_refuse_stale_model_capabilities(tmp_path, change):
    adapter = _Adapter()
    runner = _Runner(adapter, tmp_path)
    providers, model, provider, key, home = await runner._provider_inventory_snapshot(_source())
    dashboard = dashboard_payload(current_model=model, current_provider=provider, providers=providers,
                                  settings={}, reasoning_effort="")
    await adapter.ui.open_dashboard(_source(), dashboard, providers, model, provider, key, home)
    state = next(iter(adapter.ui._states.values()))
    if change == "config":
        (tmp_path / "config.yaml").write_text("model:\n  default: different-model\n")
    else:
        runner._session_model_overrides[key] = {"model": "different-model", "provider": "other"}
    callback = _callback(state["message_id"], 555, f"ms:{state['id']}:settings")
    await adapter.ui.handle_callback(callback, _source(), f"ms:{state['id']}:settings")
    assert callback.answers[-1][1] is True
    assert "configuration changed" in callback.answers[-1][0]
    assert len(adapter.requests) == 1
    assert runner.sessions[key].conversation.model_settings == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_pending_reply_cleanup_uses_real_owner_handler_and_edits_in_place(tmp_path, accepted):
    from unittest.mock import AsyncMock
    from telethon.tl.functions.messages import EditMessageRequest

    (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=42\n")
    adapter = _Adapter()
    adapter._client = SimpleNamespace(delete_messages=AsyncMock())
    runner = _Runner(adapter, tmp_path)
    runner._model_settings_owner_error = GatewayModelCommandsMixin._model_settings_owner_error.__get__(runner)
    source = _source()
    event = MessageEvent(text="/msettings", message_type=MessageType.COMMAND, source=source)
    assert await runner._handle_msettings_command(event) is None
    state = next(iter(adapter.ui._states.values()))
    original_id = state["message_id"]
    await adapter.ui.handle_callback(_callback(original_id, 991, ""), source,
                                     f"ms:{state['id']}:input:temperature")
    if not accepted:
        # UI identity is still valid, but the gateway rechecks the actual profile owner.
        (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=99\n")
    before = len(adapter.requests)
    inbound = SimpleNamespace(message=SimpleNamespace(id=8888))
    assert await adapter.ui.handle_message(inbound, source, "2")
    assert not adapter.ui._pending
    assert state["message_id"] == original_id
    assert len(adapter.requests) == before + 1
    assert isinstance(adapter.requests[-1], EditMessageRequest)
    assert adapter.requests[-1].id == int(original_id)
    assert adapter.requests[-1].rich_message.html.rstrip().endswith("<p><i>Page 1 of 3 • total 4</i></p>")
    settings = runner.sessions["session-A"].conversation.model_settings
    if accepted:
        assert settings == {"temperature": 2.0}
        adapter._client.delete_messages.assert_awaited_once_with(("peer", "42"), [8888], revoke=True)
    else:
        assert settings == {}
        adapter._client.delete_messages.assert_not_awaited()
        assert "owner" in adapter.requests[-1].rich_message.html.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("action,expected_view", [("providers", "providers")])
async def test_dashboard_provider_catalog_handoff_edits_same_card_without_duplicates(tmp_path, monkeypatch, action, expected_view):
    """Exercise real /model routing and the adapter handoff, not just a mock dispatch."""
    from telethon.tl.functions.messages import EditMessageRequest, SendMessageRequest
    from plugins.platforms.telegram.provider_management_ui import TelegramProviderManagementUI
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
    import gateway.run

    monkeypatch.setattr(gateway.run, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("model:\n  default: model-x\n  provider: openrouter\n")
    adapter = _Adapter()
    provider_ui = TelegramProviderManagementUI(adapter)
    adapter._provider_ui = lambda: provider_ui
    adapter.send_provider_management = TelethonAdapter.send_provider_management.__get__(adapter)
    runner = _Runner(adapter, tmp_path)
    source = _source()
    providers, model, provider, key, home = await runner._provider_inventory_snapshot(source)
    providers[0]["models"] = ["model-x", "model-y"]
    original_inventory = runner._provider_inventory_snapshot

    async def inventory(src):
        rows, m, p, k, h = await original_inventory(src)
        rows[0]["models"] = ["model-x", "model-y"]
        return rows, m, p, k, h

    runner._provider_inventory_snapshot = inventory
    await adapter.ui.open_dashboard(source, dashboard_payload(
        current_model=model, current_provider=provider, providers=providers,
        settings={}, reasoning_effort=""), providers, model, provider, key, home)
    state = next(iter(adapter.ui._states.values()))
    original_id = state["message_id"]
    data = f"ms:{state['id']}:{action}"
    callback = _callback(original_id, 1100, data)
    await adapter.ui.handle_callback(callback, source, data)
    assert len(provider_ui._states) == 1
    provider_state = next(iter(provider_ui._states.values()))
    assert provider_state["view"] == expected_view
    assert str(provider_state["message_id"]) == original_id
    assert len(adapter.requests) == 2
    assert isinstance(adapter.requests[0], SendMessageRequest)
    assert isinstance(adapter.requests[1], EditMessageRequest)
    assert adapter.requests[1].id == int(original_id)
    # Telegram may redeliver a query; do not hand off twice or duplicate UI state.
    await adapter.ui.handle_callback(callback, source, data)
    assert len(adapter.requests) == 2
    assert len(provider_ui._states) == 1


@pytest.mark.asyncio
async def test_list_change_uses_pv_models_callback_and_restores_topic_on_exact_card(tmp_path, monkeypatch):
    from telethon import types
    from telethon.tl.functions.messages import EditMessageRequest, SendMessageRequest
    from plugins.platforms.telegram.provider_management_ui import TelegramProviderManagementUI
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
    import gateway.run

    monkeypatch.setattr(gateway.run, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("model:\n  default: model-x\n  provider: openrouter\n")
    adapter = _Adapter()
    provider_ui = TelegramProviderManagementUI(adapter)
    adapter._provider_ui = lambda: provider_ui
    adapter.send_provider_management = TelethonAdapter.send_provider_management.__get__(adapter)
    runner = _Runner(adapter, tmp_path)
    source = _source(thread="17")
    providers, model, provider, key, home = await runner._provider_inventory_snapshot(source)
    providers[0]["models"] = ["model-x", "model-y"]
    dashboard = dashboard_payload(
        current_model=model, current_provider=provider, providers=providers,
        settings={}, reasoning_effort="",
    )

    assert await adapter.ui.open_dashboard(
        source, dashboard, providers, model, provider, key, home, invocation_message_id=900,
    )
    model_state = next(iter(adapter.ui._states.values()))
    provider_state = next(iter(provider_ui._states.values()))
    original_id = model_state["message_id"]
    assert isinstance(adapter.requests[0], SendMessageRequest)
    assert adapter.requests[0].reply_to.reply_to_msg_id == 17
    assert adapter.requests[0].reply_to.top_msg_id == 17
    list_change = next(
        button for row in adapter.requests[0].reply_markup.rows for button in row.buttons
        if button.text == "List/change"
    )
    data = list_change.type.data.decode("utf-8")
    assert data == f"pv:{provider_state['id']}:models:0"
    assert data != f"ms:{model_state['id']}:models"
    assert provider_state["message_id"] == original_id

    # Telegram may omit the topic in the callback source; exact chat, user, and card identity bind it.
    callback = _callback(original_id, 1101, data)
    assert await provider_ui.handle_callback(callback, _source(thread=None), data)
    assert callback.answers == [("", False)]
    assert provider_state["view"] == "models"
    assert provider_state["selected_provider"] == "openrouter"
    assert isinstance(adapter.requests[1], EditMessageRequest)
    assert adapter.requests[1].id == int(original_id)
    assert isinstance(adapter.requests[1].rich_message, types.InputRichMessage)
    assert len(adapter.requests[1].rich_message._bytes()) > 0
    assert len(adapter.requests[1].reply_markup._bytes()) > 0
    model_actions = [button.type.data.decode("utf-8")
                     for row in adapter.requests[1].reply_markup.rows for button in row.buttons]
    assert f"pv:{provider_state['id']}:select:0" in model_actions


@pytest.mark.asyncio
async def test_reply_anchor_alone_cannot_request_provider_card_edit(tmp_path):
    from telethon.tl.functions.messages import SendMessageRequest
    from plugins.platforms.telegram.provider_management_ui import TelegramProviderManagementUI
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    adapter = _Adapter()
    _Runner(adapter, tmp_path)
    provider_ui = TelegramProviderManagementUI(adapter)
    adapter._provider_ui = lambda: provider_ui
    await TelethonAdapter.send_provider_management(
        adapter, source=_source(), providers=[], current_model="m", current_provider="p",
        session_key="session-A", profile_home=tmp_path, reply_to=12345)
    assert len(adapter.requests) == 1
    assert isinstance(adapter.requests[0], SendMessageRequest)
    assert adapter.requests[0].reply_to.reply_to_msg_id == 12345


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/model", "/msettings", "/providers", "/models"])
async def test_native_model_close_tracks_command_not_task_reply_anchor(tmp_path, monkeypatch, command):
    from unittest.mock import AsyncMock
    from plugins.platforms.telegram.provider_management_ui import TelegramProviderManagementUI
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
    import gateway.run

    monkeypatch.setattr(gateway.run, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("model:\n  default: model-x\n  provider: openrouter\n")
    adapter = _Adapter()
    adapter._client = SimpleNamespace(delete_messages=AsyncMock())
    provider_ui = TelegramProviderManagementUI(adapter)
    adapter._provider_ui = lambda: provider_ui
    adapter.send_provider_management = TelethonAdapter.send_provider_management.__get__(adapter)
    adapter.delete_message = TelethonAdapter.delete_message.__get__(adapter)
    runner = _Runner(adapter, tmp_path)
    runner._reply_anchor_for_event = lambda _event: "888"
    source = _source()
    event = MessageEvent(text=command, message_type=MessageType.COMMAND, source=source, message_id="777")
    handler = {
        "/model": runner._handle_model_command, "/msettings": runner._handle_msettings_command,
        "/providers": runner._handle_providers_command, "/models": runner._handle_models_command,
    }[command]
    await handler(event)
    ui = provider_ui if command in {"/providers", "/models"} else adapter.ui
    state = next(iter(ui._states.values()))
    assert int(state["invocation_message_id"]) == 777
    menu_id = int(state["message_id"])
    prefix = "pv" if ui is provider_ui else "ms"
    data = f"{prefix}:{state['id']}:close"
    await ui.handle_callback(_callback(menu_id, 200, data), source, data)
    calls = adapter._client.delete_messages.await_args_list
    deleted = [int(item) for call in calls for item in call.args[1]]
    assert sorted(deleted) == sorted([menu_id, 777])
    assert 888 not in deleted
