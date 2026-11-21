"""Model/provider controls in groups: no limits for any admitted user.

The live failure: the Providers view opens in a group, but every tap inside it
answers "This control is not authorized for your account or chat scope.", and
the same popup fires on /models catalog taps. The adapter callback gate denied
``pv:``/``mp:`` in any group (catch-all demands a DM) and ``ms:`` demanded the
launch owner; the provider UI then refused members at open and credential
actions even for the owner, and never consumed a typed key from a group
message. These tests pin the no-limits contract: any admitted user (owner or
allowgc member) can open, tap, switch and add credentials in a group, the
switch lands on the GROUP session override only, a typed secret is consumed
and deleted, and the catch-all still denies every other prefix.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram.provider_management_ui import TelegramProviderManagementUI
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
from plugins.platforms.telegram.telethon_adapter import PlatformConfig
from tests.plugins.platforms.telegram.test_provider_management_ui import (
    CallbackEvent,
    FakeAdapter,
    IncomingEvent,
    Source,
    _provider,
    _rich_text,
    _state,
)
from tests.plugins.platforms.telegram.test_provider_management_ui_groups import (
    DM_SESSION_KEY,
    GROUP_CHAT_ID,
    GROUP_OWNER_ID,
    GROUP_SESSION_KEY,
    _group_owner_adapter,
    _group_source,
    _nous_provider,
)

MEMBER_ID = "999"
DENIAL = "This control is not authorized for your account or chat scope."


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
async def test_owner_provider_row_tap_in_group_reaches_handler(monkeypatch, tmp_path):
    """(a) The owner tapping a pv: provider row in a group reaches the UI."""
    adapter = _multiuser_adapter(monkeypatch, tmp_path)
    source = SimpleNamespace(chat_id="-1001", user_id="7", chat_type="group", thread_id=None)
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(adapter, "_control_routes", dict)
    handler = AsyncMock(return_value=True)
    monkeypatch.setattr(adapter, "_provider_ui", lambda: SimpleNamespace(handle_callback=handler))
    event, answered = _callback_event(b"pv:17:models:0")

    await adapter._on_callback_query(event)

    handler.assert_awaited_once_with(event, source, "pv:17:models:0")
    assert answered == []


@pytest.mark.asyncio
async def test_member_provider_tap_in_group_reaches_handler(monkeypatch, tmp_path):
    """(b) An admitted member tapping a pv: control in a group reaches the UI."""
    adapter = _multiuser_adapter(monkeypatch, tmp_path)
    source = SimpleNamespace(chat_id="-1001", user_id="999", chat_type="group", thread_id=None)
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(adapter, "_control_routes", dict)
    handler = AsyncMock(return_value=True)
    monkeypatch.setattr(adapter, "_provider_ui", lambda: SimpleNamespace(handle_callback=handler))
    event, answered = _callback_event(b"pv:17:models:0")

    await adapter._on_callback_query(event)

    handler.assert_awaited_once_with(event, source, "pv:17:models:0")
    assert answered == []


@pytest.mark.asyncio
async def test_member_model_picker_tap_in_group_reaches_handler(monkeypatch, tmp_path):
    """(b) An admitted member tapping an mp: picker control in a group reaches it."""
    adapter = _multiuser_adapter(monkeypatch, tmp_path)
    source = SimpleNamespace(chat_id="-1001", user_id="999", chat_type="group", thread_id=None)
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(adapter, "_control_routes", dict)
    handler = AsyncMock()
    monkeypatch.setattr(adapter, "_handle_model_picker_callback", handler)
    event, answered = _callback_event(b"mp:17:provider:0")

    await adapter._on_callback_query(event)

    handler.assert_awaited_once_with(event, "mp:17:provider:0")
    assert answered == []


@pytest.mark.asyncio
async def test_member_model_settings_tap_in_group_reaches_handler(monkeypatch, tmp_path):
    """(b) An admitted member tapping an ms: control in a group reaches the UI."""
    adapter = _multiuser_adapter(monkeypatch, tmp_path)
    source = SimpleNamespace(chat_id="-1001", user_id="999", chat_type="group", thread_id=None)
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(adapter, "_control_routes", dict)
    handler = AsyncMock(return_value=True)
    monkeypatch.setattr(adapter, "_model_settings_ui", lambda: SimpleNamespace(handle_callback=handler))
    event, answered = _callback_event(b"ms:17:settings")

    await adapter._on_callback_query(event)

    handler.assert_awaited_once_with(event, source, "ms:17:settings")
    assert answered == []


@pytest.mark.asyncio
async def test_unrelated_prefix_still_denies_non_owner_in_group(monkeypatch, tmp_path):
    """(e) The catch-all is intact: an unrelated prefix still denies a member."""
    adapter = _multiuser_adapter(monkeypatch, tmp_path)
    source = SimpleNamespace(chat_id="-1001", user_id="999", chat_type="group", thread_id=None)
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(adapter, "_control_routes", dict)
    event, answered = _callback_event(b"xx:1:2")

    await adapter._on_callback_query(event)

    assert answered == [((DENIAL,), {"alert": True})]


@pytest.mark.asyncio
async def test_member_model_switch_in_group_moves_group_override_only(tmp_path, monkeypatch):
    """(c) A member switching provider/model in a group changes the GROUP
    session override only: the profile default and the DM override are untouched."""
    import yaml
    from gateway.slash_commands_model import GatewayModelCommandsMixin
    from hermes_cli.model_switch import ModelSwitchResult

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "model": {
            "default": "vendor/model-a", "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1", "api_mode": "openai-chat-completions",
        },
    }), encoding="utf-8")
    (tmp_path / ".env").write_text(f"OWNER_TELEGRAM_ID={GROUP_OWNER_ID}\n", encoding="utf-8")
    adapter = _group_owner_adapter()
    runner = object.__new__(GatewayModelCommandsMixin)
    runner._session_model_overrides = {
        DM_SESSION_KEY: {"model": "meituan/longcat-2.5", "provider": "nous", "api_key": None,
                         "base_url": None, "api_mode": None, "request_overrides": {}},
    }
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._evict_cached_agent = lambda _session_key: None
    runner._switch_cached_agent_model = lambda _result, _ctx, _picker: None
    stored_session_overrides = {}

    class SessionStore:
        async def set_model_override(self, session_key, value):
            stored_session_overrides[session_key] = dict(value) if value is not None else None

    runner.async_session_store = SessionStore()
    runner._normalize_source_for_session_key = lambda value: value

    async def perform(_ctx, model, provider, _source):
        return ModelSwitchResult(
            success=True, new_model=model, target_provider=provider,
            provider_label="Nous", base_url="https://inference-api.nousresearch.com",
            api_mode="openai-chat-completions", is_global=True,
        ), None

    async def confirm(_result, _ctx, *, one_turn, picker, global_error=None):
        assert not one_turn and picker
        return "Model route committed."

    runner._perform_model_switch = perform
    runner._model_switch_confirmation = confirm
    adapter.gateway_runner = runner
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: [])
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda _provider: False)
    member = _group_source(user_id=MEMBER_ID)
    assert await ui.open(member, [_provider(), _nous_provider()],
                         current_model="vendor/model-a", current_provider="openrouter",
                         session_key=GROUP_SESSION_KEY, profile_home=tmp_path,
                         view="models", selected_provider="nous")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), member, f"pv:{state['id']}:select:0")
    state = _state(ui)
    assert state["current_model"] == "meituan/longcat-2.5"
    assert state["current_provider"] == "nous"
    # A member never pins the profile default: config.yaml is untouched and
    # the GROUP session override carries the new route instead.
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["model"]["default"] == "vendor/model-a"
    assert saved["model"]["provider"] == "openrouter"
    assert stored_session_overrides[GROUP_SESSION_KEY]["model"] == "meituan/longcat-2.5"
    assert stored_session_overrides[GROUP_SESSION_KEY]["provider"] == "nous"
    assert GROUP_SESSION_KEY in runner._session_model_overrides
    # The DM session was never touched by the group switch.
    assert runner._session_model_overrides[DM_SESSION_KEY]["model"] == "meituan/longcat-2.5"
    assert DM_SESSION_KEY not in stored_session_overrides


@pytest.mark.asyncio
async def test_group_message_without_thread_id_does_not_crash_pending_lookup(tmp_path):
    """Regression: widening the pending-input gate to groups must not crash
    on a source without thread_id — the lookup returns False, no consumption."""
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    source = SimpleNamespace(chat_id=GROUP_CHAT_ID, user_id=MEMBER_ID, chat_type="group")
    assert await ui.handle_message(IncomingEvent(91), source, "/help") is False
    assert not ui._pending_inputs
    assert adapter.deleted == []


@pytest.mark.asyncio
async def test_member_api_key_flow_in_group_stores_key_and_deletes_message(tmp_path, monkeypatch):
    """(d) A member adds a credential from a group: the typed key is consumed
    by the pending-input flow, stored, and the group message is deleted."""
    from hermes_cli import auth_commands

    stored = []
    (tmp_path / ".env").write_text(f"OWNER_TELEGRAM_ID={GROUP_OWNER_ID}\n", encoding="utf-8")
    adapter = _group_owner_adapter()

    def store(provider, secret, added_by=None):
        assert adapter.deleted == []
        assert added_by == MEMBER_ID
        stored.append((provider, secret))
        return {"id": "cred1", "label": "key-1"}

    monkeypatch.setattr(auth_commands, "add_api_key_credential_for_ui", store)
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: [])
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda _provider: False)
    member = _group_source(user_id=MEMBER_ID)
    assert await ui.open(member, [_provider()], current_model="vendor/model-a",
                         current_provider="openrouter", session_key=GROUP_SESSION_KEY,
                         profile_home=tmp_path)
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), member, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), member, f"pv:{state['id']}:keys")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), member, f"pv:{state['id']}:add-key")
    state = _state(ui)
    secret = "sk-test-never-display-this"
    assert await ui.handle_message(
        IncomingEvent(91), Source(chat_id=GROUP_CHAT_ID, user_id=MEMBER_ID,
                                  chat_type="group", message_id="91"), secret)
    assert adapter.deleted == [(GROUP_CHAT_ID, "91")]
    assert stored == [("openrouter", secret)]
    state = _state(ui)
    assert ui._provider(state)["ready"] is True
    rendered = _rich_text(adapter.requests[-1])
    assert secret not in rendered
    assert "credential added" in rendered.lower()
    assert secret not in repr(ui._states)
