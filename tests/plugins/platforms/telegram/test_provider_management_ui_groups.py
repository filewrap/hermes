"""Provider management in groups: open for any admitted user, group-session switch.

The providers view hangs off the /model dashboard, which already allows groups; the
view itself used to dead-end every group open into a "direct chat" warning. These
tests pin the group contract: any admitted user (owner or member) opens the GROUP
session's providers and switches the GROUP route, and credential input works from
a group card with the typed secret deleted after it is consumed.
"""

import asyncio

import pytest

from plugins.platforms.telegram.provider_management_ui import TelegramProviderManagementUI
from tests.plugins.platforms.telegram.test_provider_management_ui import (
    CallbackEvent,
    FakeAdapter,
    Source,
    _ModelRunner,
    _provider,
    _rich_text,
    _state,
)

GROUP_CHAT_ID = "-1002246015453"
GROUP_OWNER_ID = "6802929470"
GROUP_SESSION_KEY = "agent:gc-1002246015453:telegram:group:-1002246015453"
DM_SESSION_KEY = "agent:main:telegram:dm:6802929470"


def _group_owner_adapter():
    adapter = FakeAdapter()
    adapter._is_owner_user = lambda user_id, source=None: str(user_id or "") == GROUP_OWNER_ID
    return adapter


def _group_source(user_id=GROUP_OWNER_ID):
    return Source(chat_id=GROUP_CHAT_ID, user_id=user_id, chat_type="group")


def _nous_provider(**updates):
    row = _provider(
        slug="nous",
        name="Nous",
        description="Route requests through the Nous Portal API.",
        auth_type="api_key",
        api_key_env_vars=["NOUS_API_KEY"],
        base_url_env_var="",
        signup_url="https://portal.nousresearch.com",
        is_catalog_provider=True,
        configured=True,
        is_current=False,
        is_profile_default=False,
        profile_default_model="",
        models=["meituan/longcat-2.5", "nous/other-model"],
        total_models=2,
        default_model="meituan/longcat-2.5",
        default_reasoning="",
    )
    row.update(updates)
    return row


@pytest.mark.asyncio
async def test_provider_management_opens_in_group_for_owner(tmp_path):
    adapter = _group_owner_adapter()
    ui = TelegramProviderManagementUI(adapter)
    assert await ui.open(_group_source(), [_provider(), _nous_provider()],
                         current_model="vendor/model-a", current_provider="openrouter",
                         session_key=GROUP_SESSION_KEY, profile_home=tmp_path)
    assert len(adapter.requests) == 1
    rendered = _rich_text(adapter.requests[0])
    assert "direct chat" not in rendered.lower()
    assert "vendor/model-a" in rendered
    assert "total 2" in rendered
    assert "SECRET_MUST_NEVER_ENTER_UI_STATE" not in rendered
    labels = [button.text for row in adapter.requests[0].reply_markup.rows
              for button in row.buttons]
    assert any("OpenRouter" in label for label in labels)
    assert any(label.startswith("Nous") for label in labels)
    state = _state(ui)
    assert state["session_key"] == GROUP_SESSION_KEY
    assert state["profile_home"] == str(tmp_path)
    assert state["chat_id"] == GROUP_CHAT_ID
    assert state["user_id"] == GROUP_OWNER_ID
    assert "SECRET_MUST_NEVER_ENTER_UI_STATE" not in repr(state["providers"])


@pytest.mark.asyncio
async def test_provider_model_list_opens_in_group_for_owner(tmp_path):
    adapter = _group_owner_adapter()
    ui = TelegramProviderManagementUI(adapter)
    assert await ui.open(_group_source(), [_provider(), _nous_provider()],
                         current_model="vendor/model-a", current_provider="openrouter",
                         session_key=GROUP_SESSION_KEY, profile_home=tmp_path,
                         view="models", selected_provider="nous")
    rendered = _rich_text(adapter.requests[0])
    assert "meituan/longcat-2.5" in rendered
    labels = [button.text for row in adapter.requests[0].reply_markup.rows
              for button in row.buttons]
    assert "meituan/longcat-2.5" in labels
    assert "nous/other-model" in labels
    state = _state(ui)
    assert state["view"] == "models"
    assert state["session_key"] == GROUP_SESSION_KEY


@pytest.mark.asyncio
async def test_provider_model_switch_in_group_moves_group_route_and_leaves_dm_alone(tmp_path, monkeypatch):
    import yaml
    from hermes_cli.model_switch import ModelSwitchResult

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "model": {
            "default": "vendor/model-a", "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1", "api_mode": "openai-chat-completions",
        },
    }), encoding="utf-8")
    adapter = _group_owner_adapter()
    runner = object.__new__(_ModelRunner)
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
    group = _group_source()
    assert await ui.open(group, [_provider(), _nous_provider()],
                         current_model="vendor/model-a", current_provider="openrouter",
                         session_key=GROUP_SESSION_KEY, profile_home=tmp_path,
                         view="models", selected_provider="nous")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), group, f"pv:{state['id']}:select:0")
    state = _state(ui)
    assert state["current_model"] == "meituan/longcat-2.5"
    assert state["current_provider"] == "nous"
    # The owner switch pins the group profile default (config.yaml) and drops
    # the group session override (#100314: config is the single authority).
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["model"]["default"] == "meituan/longcat-2.5"
    assert saved["model"]["provider"] == "nous"
    assert stored_session_overrides[GROUP_SESSION_KEY] is None
    assert GROUP_SESSION_KEY not in runner._session_model_overrides
    # The DM session was never touched by the group switch.
    assert runner._session_model_overrides[DM_SESSION_KEY]["model"] == "meituan/longcat-2.5"
    assert DM_SESSION_KEY not in stored_session_overrides


@pytest.mark.asyncio
async def test_provider_credential_actions_work_in_group(tmp_path, monkeypatch):
    """No-limits: credential actions run from a group card — add-key and
    search arm their pending inputs, and oauth starts its device flow."""
    import threading
    from hermes_cli import auth_commands

    approved = threading.Event()

    def fake_auth_add(args):
        args.on_verification("https://login.example.test/device", "ABCD-EFGH")
        if not approved.wait(3):
            raise TimeoutError("approval timed out")

    monkeypatch.setattr(auth_commands, "auth_add_command", fake_auth_add)
    adapter = _group_owner_adapter()
    ui = TelegramProviderManagementUI(adapter)
    group = _group_source()
    assert await ui.open(group, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
                         session_key=GROUP_SESSION_KEY, profile_home=tmp_path)
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), group, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), group, f"pv:{state['id']}:add-key")
    state = _state(ui)
    assert "direct chat" not in _rich_text(adapter.requests[-1]).lower()
    assert ui._pending_inputs[ui._user_key(group)]["kind"] == "api-key"
    assert state["view"] == "collect-key"
    assert await ui.handle_callback(CallbackEvent(55), group, f"pv:{state['id']}:search")
    state = _state(ui)
    assert "direct chat" not in _rich_text(adapter.requests[-1]).lower()
    assert ui._pending_inputs[ui._user_key(group)]["kind"] == "search"
    assert await ui.handle_callback(CallbackEvent(55), group, f"pv:{state['id']}:keys")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), group, f"pv:{state['id']}:oauth")
    state = _state(ui)
    assert state["view"] == "oauth"
    from tests.waiting import async_wait_until

    await async_wait_until(lambda: state.get("oauth_code"), timeout=3, what="OAuth verification code")
    assert state.get("oauth_code") == "ABCD-EFGH"
    assert "login.example.test/device" in _rich_text(adapter.requests[-1])
    approved.set()
    task = next(iter(ui._oauth_jobs.values()))
    await asyncio.wait_for(task, timeout=3)
    assert state["view"] == "provider"
    assert "ABCD-EFGH" not in _rich_text(adapter.requests[-1])
